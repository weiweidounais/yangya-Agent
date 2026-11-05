"""Mandatory ownership predicates for ORM reads, bulk changes and flushes."""
from contextlib import contextmanager
import sqlite3

from sqlalchemy import and_, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, with_loader_criteria

from app.data_access import NotFound, current_user
from app.data_access.public import check_public_model_write, check_public_statement


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(connection, connection_record):
    # Includes private staging engines and synthetic test engines; leave PG alone.
    if isinstance(connection, sqlite3.Connection):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


# Private business stores. Audit/public tables retain their own policy.
_PRIVATE_TABLES = {
    "user_profiles", "episodic_events", "confirm_decisions",
    "confirm_tokens", "channel_conversations",
    "channel_bindings", "channel_link_tokens", "user_secrets",
    "user_schedules", "user_schedule_runs", "user_schedule_proposals", "user_activity",
    "proactive_items", "proactive_runs", "memory_facts", "memory_migration_receipts", "turn_results",
}


def related_owner(model):
    """Ownership for rows whose owner lives on their parent record."""
    name = getattr(getattr(model, "__table__", None), "name", None)
    if name == "channel_conversations":
        from app.channels.binding import ChannelBinding
        return ChannelBinding, ("channel", "external_user_id"), "user_id"
    return None


def related_predicate(model, uid):
    from sqlalchemy import exists
    parent, keys, owner = related_owner(model)
    return exists().where(*(getattr(parent, key) == getattr(model, key) for key in keys),
                          getattr(parent, owner) == uid)


def private_model(model):
    return owner_column(model) is not None or related_owner(model) is not None


def check_related_owner(session, row):
    from sqlalchemy import select
    parent, keys, owner = related_owner(type(row))
    uid = current_user()
    if uid is not None and session.scalar(select(parent).where(
        *(getattr(parent, key) == getattr(row, key) for key in keys),
        getattr(parent, owner) == uid,
    )) is None:
        raise NotFound()


def owner_column(model):
    table = getattr(model, "__table__", None)
    if table is None or table.name not in _PRIVATE_TABLES:
        return None
    if "user_id" in table.c:
        return model.user_id
    return None


class ScopedSession(Session):
    def get_bind(self, mapper=None, *, clause=None, bind=None, **kwargs):
        resolved = super().get_bind(mapper, clause=clause, bind=bind, **kwargs)
        system_engine = self.info.get("system_engine")
        if system_engine is not None and resolved is self.bind:
            from app.data_access.postgres import trusted_identity
            if trusted_identity() is None:
                return system_engine
        return resolved

    def bulk_insert_mappings(self, mapper, mappings, **kwargs):
        model = inspect(mapper).class_
        check_public_model_write(model)
        if related_owner(model) and current_user() is not None:
            raise NotFound()
        column = owner_column(model)
        mappings = list(mappings)
        if column is not None:
            uid = current_user()
            if uid is not None and any(row.get(column.key) != uid for row in mappings):
                raise NotFound()
        return super().bulk_insert_mappings(mapper, mappings, **kwargs)

    def bulk_update_mappings(self, mapper, mappings):
        # Legacy bulk APIs bypass both flush and do_orm_execute predicates.
        check_public_model_write(inspect(mapper).class_)
        if private_model(inspect(mapper).class_) and current_user() is not None:
            raise NotFound()
        return super().bulk_update_mappings(mapper, mappings)

    def bulk_save_objects(self, objects, **kwargs):
        objects = list(objects)
        for obj in objects:
            check_public_model_write(type(obj))
            if related_owner(type(obj)) and current_user() is not None:
                raise NotFound()
            column = owner_column(type(obj))
            if column is not None:
                uid = current_user()
                if uid is not None and (inspect(obj).key is not None or getattr(obj, column.key) != uid):
                    raise NotFound()
        return super().bulk_save_objects(objects, **kwargs)

    def refresh(self, instance, *args, **kwargs):
        column = owner_column(type(instance))
        if column is not None:
            uid = current_user()
            if uid is not None and getattr(instance, column.key) != uid:
                raise NotFound()
        if related_owner(type(instance)):
            check_related_owner(self, instance)
        return super().refresh(instance, *args, **kwargs)

    def get(self, entity, ident, **kwargs):
        column = owner_column(entity)
        uid = current_user() if column is not None else None
        row = super().get(entity, ident, **kwargs)
        if row is not None and related_owner(entity):
            try:
                check_related_owner(self, row)
            except NotFound:
                return None
        if row is not None and column is not None and uid is not None:
            if getattr(row, column.key) != uid:
                return None
        return row


def check_public_execution(connection, cursor, statement, parameters, context, executemany):
    from sqlalchemy import text

    compiled = context.compiled
    check_public_statement(compiled.statement if compiled is not None else text(statement))


@event.listens_for(ScopedSession, "after_begin")
def guard_public_connection(session, transaction, connection):
    from app.data_access.postgres import begin_transaction
    if not transaction.nested:
        begin_transaction(connection)
    # Flush-generated secondary-table DML bypasses do_orm_execute and may
    # involve no mapped public object. Check the SQL on every session bind.
    if not event.contains(connection, "before_cursor_execute", check_public_execution):
        event.listen(connection, "before_cursor_execute", check_public_execution)


@event.listens_for(ScopedSession, "do_orm_execute")
def scope_statement(state):
    check_public_statement(state.statement)
    from app.db import Base
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.selectable import Select, Alias
    # Check each SELECT independently: an outer ORM mapper must not authorize
    # an unscoped Core reference in a nested query.
    sync_mapper = None
    has_private_mapper = False
    for clause in visitors.iterate(state.statement):
        if not isinstance(clause, Select) and clause is not state.statement:
            continue
        projections = getattr(clause, "_raw_columns", ())
        mapper = next((p._annotations.get("parentmapper") for p in projections
                       if p._annotations.get("identity_token")), None)
        if clause is state.statement:
            sync_mapper = mapper
        local, stack = [], [clause]
        while stack:
            node = stack.pop()
            if node is not clause and isinstance(node, Select):
                continue
            local.append(node)
            if not node._annotations.get("parentmapper"):
                stack.extend(node.get_children())
        mapped_tables = {
            getattr(n._annotations.get("parententity"), "selectable",
                    n._annotations["parentmapper"].local_table)
            for n in local if n._annotations.get("parentmapper")
        }
        has_private_mapper |= any(private_model(n._annotations["parentmapper"].class_)
                                  for n in local if n._annotations.get("parentmapper"))
        for node in local:
            table = getattr(node, "table", node)
            if (getattr(table, "name", None) in _PRIVATE_TABLES
                    and not node._annotations.get("parentmapper")
                    and table not in mapped_tables):
                current_user()
                raise NotFound()
            if (isinstance(table, Alias) and not node._annotations.get("parentmapper")
                    and any(getattr(child, "name", None) in _PRIVATE_TABLES
                            for child in visitors.iterate(table))):
                current_user()
                raise NotFound()
        for projection in projections:
            table = getattr(projection, "table", projection)
            if (getattr(table, "name", None) in _PRIVATE_TABLES
                    and not projection._annotations.get("parentmapper")
                    and not (mapper is not None and projection in mapper.primary_key)):
                current_user()
                raise NotFound()
    if not has_private_mapper:
        return
    uid = current_user()
    if uid is None:
        return
    statement = state.statement
    if state.is_select:
        if sync_mapper is not None:
            column = owner_column(sync_mapper.class_)
            if column is not None:
                statement = statement.where(column == uid)
            elif related_owner(sync_mapper.class_):
                statement = statement.where(related_predicate(sync_mapper.class_, uid))
        for mapper in Base.registry.mappers:
            column = owner_column(mapper.class_)
            if related_owner(mapper.class_):
                statement = statement.options(with_loader_criteria(
                    mapper.class_, related_predicate(mapper.class_, uid), include_aliases=True))
            if column is not None:
                statement = statement.options(with_loader_criteria(
                    mapper.class_, column == uid, include_aliases=True))
    elif state.is_insert:
        if related_owner(state.bind_mapper.class_):
            raise NotFound()  # Use ORM flush to validate the parent owner.
        column = owner_column(state.bind_mapper.class_)
        conflict = getattr(statement, "_post_values_clause", None)
        if getattr(conflict, "__visit_name__", None) == "on_conflict_do_update":
            if any(getattr(k, "key", k) == column.key for k in conflict.update_values_to_set):
                raise NotFound()
            # Guard the existing row as well as the proposed INSERT owner.
            # Clone both clauses so reusing a statement cannot retain a user's scope.
            statement = statement._generate()
            conflict = conflict._clone()
            predicate = column == uid
            if conflict.update_whereclause is not None:
                predicate = and_(conflict.update_whereclause, predicate)
            conflict.update_whereclause = predicate
            statement._post_values_clause = conflict
        values = getattr(statement, "_values", None) or {}
        batches = getattr(statement, "_multi_values", ())
        params = state.parameters
        rows = params if isinstance(params, list) else [params or {}]
        if batches:
            rows = [row for batch in batches for row in batch]
        for row in rows:
            owner = row.get(column.key)
            if owner is None:
                owner = next((v for k, v in values.items() if getattr(k, "key", k) == column.key), None)
                owner = getattr(owner, "value", owner)
            if not isinstance(owner, str) or owner != uid:
                raise NotFound()
    elif state.is_update or state.is_delete:
        mapper = state.bind_mapper
        if mapper and related_owner(mapper.class_):
            if state.is_update:
                values = getattr(statement, "_values", None) or {}
                params = state.parameters
                rows = params if isinstance(params, list) else [params or {}]
                keys = list(values) + [k for k, _ in (getattr(statement, "_ordered_values", None) or ())]
                keys += [k for row in rows for k in row]
                if any(getattr(k, "key", k) in related_owner(mapper.class_)[1] for k in keys):
                    raise NotFound()
            statement = statement.where(related_predicate(mapper.class_, uid))
        column = owner_column(mapper.class_) if mapper else None
        if state.is_update and column is not None:
            values = getattr(statement, "_values", None) or {}
            params = state.parameters
            rows = params if isinstance(params, list) else [params or {}]
            # Never allow an UPDATE to assign ownership, even via expressions.
            keys = list(values) + [k for k, _ in (getattr(statement, "_ordered_values", None) or ())]
            if any(getattr(k, "key", k) == column.key for k in keys) or any(column.key in row for row in rows):
                raise NotFound()
        if column is not None:
            statement = statement.where(column == uid)
    state.statement = statement


@event.listens_for(ScopedSession, "before_flush")
def scope_flush(session, flush_context, instances):
    rows = session.new | session.dirty | session.deleted
    for row in rows:
        check_public_model_write(type(row))
    if not any(private_model(type(row)) for row in rows):
        return
    uid = current_user()
    if uid is None:
        return
    for row in rows:
        if related_owner(type(row)):
            check_related_owner(session, row)
            if row not in session.new and any(inspect(row).attrs[k].history.has_changes()
                                              for k in related_owner(type(row))[1]):
                raise NotFound()
        column = owner_column(type(row))
        if column is not None:
            history = inspect(row).attrs[column.key].history
            if getattr(row, column.key) != uid or (row not in session.new and history.has_changes()):
                raise NotFound()


def session_factory():
    """Return the configured identity-enforcing factory (including audit binds)."""
    from app.db import SessionLocal
    return SessionLocal


def SessionLocal(*args, **kwargs):
    return session_factory()(*args, **kwargs)


# Preserve the context-manager API without creating a second engine/factory.
SessionLocal.begin = lambda: session_factory().begin()


def memory_session(existing=None):
    from app.db import memory_session as shared_session
    from contextlib import nullcontext
    if existing is not None:
        if not isinstance(existing, ScopedSession):
            raise TypeError("Private stores require ScopedSession")
        return nullcontext(existing)
    return shared_session()


@contextmanager
def write_session(existing=None, *, independent=False):
    """Reserve SQLite's writer before reading state used by a mutation.

    Join the caller's scoped transaction/workspace, including its rollback.
    Independent stores retain their existing standalone commit boundary.
    SQLite's reservation, not a process-local lock, serializes live writers.
    """
    from sqlalchemy import text
    with SessionLocal.begin() if independent else memory_session(existing) as session:
        connection = session.connection()
        if (connection.dialect.name == "sqlite"
                and not connection.connection.driver_connection.in_transaction):
            session.execute(text("BEGIN IMMEDIATE"))
        yield session


def memory_transaction_active():
    """Readers must stay on the current thread when joining staged memory."""
    from app.db import _memory_session
    return _memory_session.get() is not None


def make_session_factory(engine, *, system_engine=None):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(class_=ScopedSession, bind=engine, autoflush=False, autocommit=False,
                        info={"system_engine": system_engine})


@contextmanager
def migration_session(engine):
    """Join speculative writes, otherwise bind a scoped migration to its DB."""
    from app.db import _memory_session
    existing = _memory_session.get()
    if existing is not None:
        yield existing
    else:
        with ScopedSession(engine) as session, session.begin():
            yield session
