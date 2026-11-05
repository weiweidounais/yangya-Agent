"""Confirm-card decisions: no expiry; later click overrides earlier.

Product lock: 确认卡不过期，后点的可覆盖.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, init_db
from app.data_access.session import SessionLocal, memory_session


class ConfirmDecision(Base):
    __tablename__ = "confirm_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    channel: Mapped[str] = mapped_column(String(64), default="web")
    edits_json: Mapped[str] = mapped_column(Text, default="{}")
    override_count: Mapped[int] = mapped_column(Integer, default=0)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    # No expires_at — cards never expire by policy.


def _ensure() -> None:
    init_db()


def record_decision(
    thread_id: str,
    user_id: str,
    confirmed: bool,
    *,
    channel: str = "web",
    edits: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Upsert latest decision. Later call always wins (override)."""
    import json

    _ensure()
    thread_id = str(thread_id or "").strip()
    user_id = str(user_id or "").strip()
    if not thread_id or not user_id:
        raise ValueError("thread_id and user_id required")
    with memory_session() as session:
        row = session.execute(
            select(ConfirmDecision).where(ConfirmDecision.thread_id == thread_id)
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        edits_s = json.dumps(edits or {}, ensure_ascii=False)
        if row is None:
            row = ConfirmDecision(
                thread_id=thread_id,
                user_id=user_id,
                confirmed=bool(confirmed),
                channel=channel or "web",
                edits_json=edits_s,
                override_count=0,
                decided_at=now,
            )
            session.add(row)
            overridden = False
        else:
            overridden = True
            row.confirmed = bool(confirmed)
            row.user_id = user_id
            row.channel = channel or row.channel
            row.edits_json = edits_s
            row.override_count = int(row.override_count or 0) + 1
            row.decided_at = now
        session.flush()
        session.refresh(row)
        return {
            "thread_id": row.thread_id,
            "user_id": row.user_id,
            "confirmed": row.confirmed,
            "channel": row.channel,
            "override_count": row.override_count,
            "overridden": overridden,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "expires": None,  # never
            "policy": "no_expiry_later_overrides",
        }


def get_decision(thread_id: str) -> Optional[dict[str, Any]]:
    import json

    _ensure()
    with memory_session() as session:
        row = session.execute(
            select(ConfirmDecision).where(ConfirmDecision.thread_id == str(thread_id))
        ).scalar_one_or_none()
        if not row:
            return None
        try:
            edits = json.loads(row.edits_json or "{}")
        except json.JSONDecodeError:
            edits = {}
        return {
            "thread_id": row.thread_id,
            "user_id": row.user_id,
            "confirmed": row.confirmed,
            "channel": row.channel,
            "override_count": row.override_count,
            "edits": edits,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "expires": None,
            "policy": "no_expiry_later_overrides",
        }


# ----- One-time, owner-bound confirm tokens (S1/#5) -----


class ConfirmToken(Base):
    __tablename__ = "confirm_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ConfirmTokenError(Exception):
    def __init__(self, message: str, code: str, http_status: int = 409):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


def _hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(thread_id: str, user_id: str) -> str:
    import secrets

    _ensure()
    tok = "ct_" + secrets.token_urlsafe(24)
    with SessionLocal() as session:
        session.add(ConfirmToken(token_hash=_hash(tok), thread_id=str(thread_id), user_id=str(user_id)))
        session.commit()
    return tok


def consume_token(token: str, thread_id: str, user_id: str) -> None:
    """Atomically burn a token. Wrong owner/thread → 403; missing/used → 409."""
    from sqlalchemy import update

    _ensure()
    tok = str(token or "").strip()
    if not tok:
        raise ConfirmTokenError("缺少确认凭证，请刷新后再点", "confirm_token_required", 400)
    h = _hash(tok)
    with SessionLocal() as session:
        row = session.execute(select(ConfirmToken).where(ConfirmToken.token_hash == h)).scalar_one_or_none()
        if row is None:
            raise ConfirmTokenError("确认凭证无效，请刷新后再点", "confirm_token_invalid", 404)
        if row.user_id != str(user_id) or row.thread_id != str(thread_id):
            raise ConfirmTokenError("确认凭证无效，请刷新后再点", "confirm_token_invalid", 404)
        res = session.execute(
            update(ConfirmToken)
            .where(ConfirmToken.token_hash == h, ConfirmToken.used_at.is_(None))
            .values(used_at=datetime.now(timezone.utc))
        )
        session.commit()
        if res.rowcount != 1:
            raise ConfirmTokenError("这次确认已经提交过了", "confirm_token_used", 409)


def issue_card_token(thread_id: str, user_id: str, state: dict) -> str:
    """Token for the confirm card currently shown on this thread."""
    return issue_token(thread_id, user_id)
