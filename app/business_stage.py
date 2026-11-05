"""Private SQLite workspace for speculative node writes.

Tools retain read-your-writes and generated IDs without locking the live DB.
Only changed rows are applied after guards succeed. Concurrent changes to those
rows (including generated-ID collisions) fail closed instead of overwriting data.
Audit writers use their own engine and never enter this workspace.
"""

import uuid
from contextlib import contextmanager

from sqlalchemy import event, inspect
from app.data_access.session import ScopedSession

from app import db


from app.data_access.staging import BusinessCommitFailure, workspace_rows as _rows, _apply, prepare_workspace


def _assign_insert_ids(session, flush_context, instances):
    # SQLite's snapshot-local MAX(id)+1 races independent inserts (including
    # background summaries). Allocate IDs before flush so returned IDs and
    # references stay valid when published. Keep integers exactly representable
    # by JSON/JavaScript consumers; the commit conflict check remains authoritative.
    for row in session.new:
        mapper = inspect(row).mapper
        column = mapper.local_table.autoincrement_column
        if column is not None and getattr(row, column.key) is None:
            setattr(row, column.key, (uuid.uuid4().int >> 76) or 1)


@contextmanager
def stage_business_writes():
    """Nest within the request's workspace; publish only at its outer boundary."""
    if db._memory_session.get() is not None:
        yield
        return

    if db.ENGINE.dialect.name == "postgresql":
        phase = "setup"
        try:
            with db.memory_transaction():
                phase = "body"
                yield
                phase = "finalization"
        except Exception as exc:
            if phase == "body":
                raise
            raise BusinessCommitFailure(f"business staging {phase} failed") from exc
        return

    engine, tables, before = prepare_workspace()
    try:
        hooks = []
        with ScopedSession(engine, autoflush=False) as session:
            event.listen(session, "before_flush", _assign_insert_ids)
            token = db._memory_session.set(session)
            htoken = db._after_commit.set(hooks)
            try:
                yield
                try:
                    session.flush()
                    changes = []
                    for table in tables:
                        before_rows = before[table.name]
                        after = _rows(session, table)
                        for key in before_rows.keys() | after.keys():
                            old, new = before_rows.get(key), after.get(key)
                            if old != new:
                                changes.append((table, key, old, new))
                except Exception as exc:
                    raise BusinessCommitFailure("business staging finalization failed") from exc
            finally:
                db._after_commit.reset(htoken)
                db._memory_session.reset(token)
        try:
            _apply(changes)
        except Exception as exc:
            raise BusinessCommitFailure("business commit failed") from exc
        for fn in hooks:
            try:
                fn()
            except Exception:
                db.logging.getLogger("app.db").warning("after-commit hook failed", exc_info=False)
    finally:
        engine.dispose()
