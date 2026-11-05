"""Privileged admin sessions and serialized user-flag transactions."""
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.data_access import system_scope
from app.data_access.session import SessionLocal


@contextmanager
def admin_session(*, write=False):
    with system_scope():
        with SessionLocal() as session:
            if write:
                if db.ENGINE.dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                elif db.ENGINE.dialect.name == "postgresql":
                    # Serialize flag updates and their journal entries.
                    session.execute(text("SELECT pg_advisory_xact_lock(17601981)"))
            yield session
            if write:
                try:
                    session.commit()
                except SQLAlchemyError:
                    # A failed COMMIT can leave the driver transaction active
                    # after SQLAlchemy marks it closed. Never reuse that connection.
                    session.invalidate()
                    raise
