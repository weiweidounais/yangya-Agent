"""One-time web links handed out in channels (bind / privacy sandbox / draft view).

Only a SHA-256 of the token is stored. Links expire; ``bind`` and ``sandbox``
are single-use, ``draft`` is read-only and reusable until expiry.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, ENGINE
from app.data_access.schema import ensure_tables
from app.data_access.session import SessionLocal

PURPOSES = ("bind", "sandbox", "draft")
MAX_ATTEMPTS = 5


class ChannelLinkToken(Base):
    __tablename__ = "channel_link_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), index=True)
    channel: Mapped[str] = mapped_column(String(64))
    external_user_id: Mapped[str] = mapped_column(String(256), index=True)
    user_id: Mapped[str] = mapped_column(String(128), default="")
    display_name: Mapped[str] = mapped_column(String(256), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


_ready = False


def _ensure() -> None:
    global _ready
    if not _ready:
        ensure_tables(ENGINE, tables=[ChannelLinkToken.__table__])
        _ready = True


def _hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row_dict(row: ChannelLinkToken) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "purpose": row.purpose,
        "channel": row.channel,
        "external_user_id": row.external_user_id,
        "user_id": row.user_id or "",
        "display_name": row.display_name or "",
        "payload": payload,
        "expires_at": _aware(row.expires_at).isoformat(),
        "attempts": int(row.attempts or 0),
    }


def issue(
    purpose: str,
    *,
    channel: str,
    external_user_id: str,
    ttl_seconds: int,
    user_id: str = "",
    display_name: str = "",
    payload: Optional[dict[str, Any]] = None,
) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown link purpose {purpose!r}")
    _ensure()
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(
            ChannelLinkToken(
                token_hash=_hash(token),
                purpose=purpose,
                channel=channel,
                external_user_id=str(external_user_id),
                user_id=user_id or "",
                display_name=display_name or "",
                payload_json=json.dumps(payload or {}, ensure_ascii=False),
                attempts=0,
                created_at=now,
                expires_at=now + timedelta(seconds=max(60, int(ttl_seconds))),
            )
        )
        session.commit()
    return token


def _live(session, token: str, purpose: str) -> Optional[ChannelLinkToken]:
    row = session.get(ChannelLinkToken, _hash(token))
    if row is None or row.purpose != purpose:
        return None
    if row.used_at is not None and purpose != "draft":
        return None
    if _aware(row.expires_at) <= datetime.now(timezone.utc):
        return None
    if int(row.attempts or 0) >= MAX_ATTEMPTS:
        return None
    return row


def peek(token: str, purpose: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    _ensure()
    with SessionLocal() as session:
        row = _live(session, token, purpose)
        return _row_dict(row) if row else None


def consume(token: str, purpose: str) -> Optional[dict[str, Any]]:
    """Atomically mark a single-use link as used; returns its data or None."""
    if not token:
        return None
    _ensure()
    with SessionLocal() as session:
        row = _live(session, token, purpose)
        if row is None:
            return None
        data = _row_dict(row)
        if purpose != "draft":
            row.used_at = datetime.now(timezone.utc)
            session.commit()
        return data


def revoke_for(channel: str, external_user_id: str, purpose: str) -> int:
    """Invalidate outstanding links of one purpose for a channel user."""
    _ensure()
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        rows = session.execute(
            select(ChannelLinkToken).where(
                ChannelLinkToken.channel == channel,
                ChannelLinkToken.external_user_id == str(external_user_id),
                ChannelLinkToken.purpose == purpose,
                ChannelLinkToken.used_at.is_(None),
            )
        ).scalars().all()
        for row in rows:
            row.used_at = now
        session.commit()
        return len(rows)
