"""SQLite (default) or Postgres persistence for user profiles (JSON blob)."""

from __future__ import annotations

from app.agent.tool_registry import tool

import json
import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import DateTime, Float, String, Text, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "profiles.db"
from app.data_access.postgres import database_engine

ENGINE = (database_engine(os.environ["DATABASE_URL"]) if os.getenv("DATABASE_URL") else
          create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}))
from app.data_access.session import make_session_factory

SessionLocal = make_session_factory(
    ENGINE, system_engine=(database_engine(os.environ["SYSTEM_DATABASE_URL"])
                           if ENGINE.dialect.name == "postgresql" and os.getenv("SYSTEM_DATABASE_URL")
                           else None))


class Base(DeclarativeBase):
    pass


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class TurnResult(Base):
    """Register recoverable turn responses with the core persistence schema."""

    __tablename__ = "turn_results"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="running")
    response_json: Mapped[str] = mapped_column(Text, default="")


_schema_lock = threading.RLock()


def init_db() -> None:
    from app.agent.schedules import models as schedule_models  # noqa: F401
    from app.admin.models import AdminChangeLog, AdminUserFlag  # noqa: F401
    from app.data_access.memory_facts import MemoryFact  # noqa: F401
    from app.data_access.memory_migration import MigrationReceipt  # noqa: F401

    if ENGINE.dialect.name == "postgresql":
        return  # Schema is provisioned by the owner migration, never the app role.

    outer = _memory_session.get()
    if outer is not None:
        # Inside BEGIN IMMEDIATE: DDL must use the same connection or it waits on our own lock.
        Base.metadata.create_all(outer.connection())
        return
    with _schema_lock, ENGINE.begin() as connection:
        if not set(Base.metadata.tables) <= set(inspect(connection).get_table_names()):
            # Workers may register models lazily at the same time. Reserve the
            # database writer before create_all rechecks and creates tables.
            connection.execute(text("BEGIN IMMEDIATE"))
        Base.metadata.create_all(connection)


_memory_lock = threading.Lock()
_memory_session: ContextVar[Optional[Session]] = ContextVar("memory_session", default=None)
_after_commit: ContextVar[Optional[list]] = ContextVar("memory_after_commit", default=None)


def after_memory_commit(fn) -> None:
    """Run fn after the enclosing memory transaction commits (now, if there is none).

    Work that opens its own connection must not run inside BEGIN IMMEDIATE, or it
    waits on our own write lock ("database is locked").
    """
    hooks = _after_commit.get()
    if hooks is None:
        fn()
    else:
        hooks.append(fn)


# Failure rollback covers user-visible business data: episodic events,
# profile/memory, drafts, and returned cards. Telemetry, cost-ledger entries,
# and traces are independent audit writes: they must persist immediately,
# without joining or waiting for this transaction.
@contextmanager
def memory_transaction():
    """Serialize revision checks and memory writes, including across processes."""
    session = _memory_session.get()
    if session is not None:
        yield session
        return
    with _memory_lock, SessionLocal() as session:
        init_db()
        if ENGINE.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            session.connection()  # Begin and install the transaction-local identity.
        token = _memory_session.set(session)
        hooks: list = []
        htoken = _after_commit.set(hooks)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            _after_commit.reset(htoken)
            _memory_session.reset(token)
    for fn in hooks:
        try:
            fn()
        except Exception:
            logging.getLogger("app.db").warning("after-commit hook failed", exc_info=False)


@contextmanager
def memory_session():
    """Join a memory transaction, or commit a standalone operation as before."""
    session = _memory_session.get()
    if session is not None:
        yield session
    else:
        with SessionLocal.begin() as session:
            yield session


@tool('profile_read', risk_level='read', description='get profile')
def get_profile(user_id: str) -> dict[str, Any]:
    return read_profile_data(user_id)


def read_profile_data(user_id: str) -> dict[str, Any]:
    """Read through the identity-scoped data session without invoking agent tools."""
    with memory_session() as session:
        row = session.get(UserProfile, user_id)
        if not row:
            return {}
        try:
            data = json.loads(row.profile_json or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


@tool('profile_save', risk_level='self_write', description='save profile')
def save_profile(user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    profile = dict(profile or {})
    with memory_transaction() as session:
        row = session.get(UserProfile, user_id)
        payload = json.dumps(profile, ensure_ascii=False)
        if row is None:
            row = UserProfile(user_id=user_id, profile_json=payload)
            session.add(row)
        else:
            row.profile_json = payload
            row.updated_at = datetime.now(timezone.utc)
        session.flush()
        updated_at = row.updated_at

    def _index() -> None:
        try:
            from app.agent.harness import identity

            identity.index_profile(user_id, profile, updated_at=updated_at)
        except Exception:
            # sync_index() backfills on the next leak scan.
            logging.getLogger("app.db").warning("identity index update failed for a profile", exc_info=False)

    after_memory_commit(_index)
    return profile


@tool('profile_update', risk_level='self_write', description='merge and save')
def merge_and_save(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a patch: lists are appended without duplicates, scalars replaced.

    Empty strings never erase an existing value (use the memory card to delete).
    """
    with memory_transaction():
        current = get_profile(user_id)
        for key, value in (patch or {}).items():
            if value is None:
                continue
            if isinstance(value, list) and isinstance(current.get(key), list):
                # de-dupe while preserving order
                seen: set[str] = set()
                merged: list[str] = []
                for item in list(current.get(key) or []) + list(value):
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                current[key] = merged
            elif isinstance(value, str) and value == "" and key in current:
                continue
            else:
                current[key] = value
        return save_profile(user_id, current)


def list_user_ids(*, limit: int = 500) -> list[str]:
    """Return known profile user_ids (for proactive fan-out)."""
    limit = max(1, min(int(limit or 500), 5000))
    with SessionLocal() as session:
        rows = session.execute(select(UserProfile.user_id).limit(limit)).scalars().all()
        return [str(r) for r in rows if r]
