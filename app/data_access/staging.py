"""Private snapshot persistence for speculative business transactions."""
import sqlite3
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from app import db


class BusinessCommitFailure(Exception):
    """Staging setup or atomic application failed; no business writes committed."""


def workspace_rows(session, table):
    """Read an owner-filtered snapshot without exposing a raw connection."""
    from app.data_access.session import ScopedSession
    if not isinstance(session, ScopedSession):
        raise TypeError("Private workspaces require ScopedSession")
    return _rows(session.connection(), table)


def _rows(connection, table):
    from app.data_access import current_user, IdentityRequired
    from app.data_access.session import _PRIVATE_TABLES
    statement = table.select()
    try:
        uid = current_user()
    except IdentityRequired:
        # An empty workspace is allowed; private reads still require identity.
        if table.name in _PRIVATE_TABLES:
            return {}
        uid = None
    if uid is not None and table.name in _PRIVATE_TABLES:
        owner = table.c.get("user_id")
        if owner is None:
            from app.data_access.session import related_predicate
            model = next(m.class_ for m in db.Base.registry.mappers if m.local_table is table)
            statement = statement.where(related_predicate(model, uid))
        else:
            statement = statement.where(owner == uid)
    rows = connection.execute(statement).mappings()
    return {tuple(row[c.name] for c in table.primary_key): dict(row) for row in rows}


def _apply(changes):
    if not changes:
        return
    # No model calls, guards, or tool execution inside this write transaction.
    with db.ENGINE.connect() as connection:
        try:
            connection.execute(text("BEGIN IMMEDIATE"))
            # Snapshot tables are in FK dependency order. Delete children first,
            # then insert/update parents first, without relying on disabled FKs.
            ordered = [change for change in reversed(changes) if change[3] is None]
            ordered += [change for change in changes if change[3] is not None]
            for table, key, old, new in ordered:
                from app.data_access import current_user, NotFound
                from app.data_access.session import _PRIVATE_TABLES
                uid = current_user()
                if uid is not None and table.name in _PRIVATE_TABLES and table.name != "channel_conversations":
                    if any(row is not None and row["user_id"] != uid for row in (old, new)):
                        raise NotFound()
                if uid is not None and table.name == "channel_conversations":
                    from app.data_access.session import related_owner
                    from sqlalchemy import select
                    model = next(m.class_ for m in db.Base.registry.mappers if m.local_table is table)
                    parent, keys, owner = related_owner(model)
                    for row in (old, new):
                        if row is not None and connection.execute(select(parent.__table__).where(
                            *(getattr(parent, k) == row[k] for k in keys),
                            getattr(parent, owner) == uid,
                        )).first() is None:
                            raise NotFound()
                predicate = [c == value for c, value in zip(table.primary_key, key)]
                current = connection.execute(table.select().where(*predicate)).mappings().first()
                if (dict(current) if current is not None else None) != old:
                    raise BusinessCommitFailure("concurrent business update")
                if old is None:
                    connection.execute(table.insert().values(**new))
                elif new is None:
                    connection.execute(table.delete().where(*predicate))
                else:
                    connection.execute(table.update().where(*predicate).values(**new))
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def prepare_workspace():
    from app.feature_flags import memory_vector_enabled

    private = sqlite3.connect(":memory:", check_same_thread=False)
    engine = create_engine("sqlite://", creator=lambda: private, poolclass=StaticPool)
    try:
        # Audit tables share metadata but live on independent engines. Legacy
        # copies in the business DB need not have their current mapped schema.
        with db.SessionLocal() as bindings:
            tables = [table for table in db.Base.metadata.sorted_tables
                      if bindings.get_bind(clause=table) is db.ENGINE
                      and (table.name != "memory_facts" or memory_vector_enabled())]
        if not {table.name for table in tables} <= set(inspect(db.ENGINE).get_table_names()):
            db.init_db()
        source = db.ENGINE.raw_connection()
        try:
            source.driver_connection.backup(private)
        finally:
            source.close()
        # Baseline is local: no live read transaction survives setup.
        with engine.connect() as connection:
            before = {table.name: _rows(connection, table) for table in tables}
    except Exception as exc:
        engine.dispose()
        raise BusinessCommitFailure("business staging setup failed") from exc

    return engine, tables, before
