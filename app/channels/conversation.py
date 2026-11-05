"""Per-channel-user conversation state.

* ``pending``: the last option menu shown (confirm / conflict / unbind …), so a
  WeChat user can reply ``1`` / ``2`` / ``确认`` instead of clicking a card.
* ``conversation_thread_id``: one agent thread per product user for chit-chat,
  shared across Feishu and WeChat. Drafts keep their own thread per request so
  confirm cards stay 1:1.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.channels.base import ChannelOption
from app.data_access.schema import ensure_tables
from app.db import Base, ENGINE
from app.data_access.session import SessionLocal


class ChannelConversation(Base):
    __tablename__ = "channel_conversations"
    __table_args__ = (UniqueConstraint("channel", "external_user_id", name="uq_conv_channel_external"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(64), index=True)
    external_user_id: Mapped[str] = mapped_column(String(256), index=True)
    pending_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


_ready = False


def _ensure() -> None:
    global _ready
    if not _ready:
        ensure_tables(ENGINE, tables=[ChannelConversation.__table__])
        _ready = True


def conversation_thread_id(user_id: str) -> str:
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:24]
    return f"chan-{digest}"


def _get_row(session, channel: str, external_user_id: str) -> Optional[ChannelConversation]:
    from app.data_access.session import check_related_owner
    check_related_owner(session, ChannelConversation(channel=channel, external_user_id=str(external_user_id)))
    return session.execute(
        select(ChannelConversation).where(
            ChannelConversation.channel == channel,
            ChannelConversation.external_user_id == str(external_user_id),
        )
    ).scalar_one_or_none()


def set_pending(
    channel: str,
    external_user_id: str,
    *,
    kind: str,
    options: list[ChannelOption],
    thread_id: str = "",
) -> None:
    _ensure()
    payload = {
        "kind": kind,
        "thread_id": thread_id,
        "options": [{"id": o.id, "label": o.label, "value": o.value} for o in options],
    }
    with SessionLocal() as session:
        row = _get_row(session, channel, external_user_id)
        if row is None:
            row = ChannelConversation(channel=channel, external_user_id=str(external_user_id))
            session.add(row)
        row.pending_json = json.dumps(payload, ensure_ascii=False)
        row.updated_at = datetime.now(timezone.utc)
        session.commit()


def get_pending(channel: str, external_user_id: str) -> dict[str, Any]:
    _ensure()
    with SessionLocal() as session:
        row = _get_row(session, channel, external_user_id)
        if row is None:
            return {}
        try:
            return json.loads(row.pending_json or "{}")
        except json.JSONDecodeError:
            return {}


def clear_pending(channel: str, external_user_id: str) -> None:
    _ensure()
    with SessionLocal() as session:
        row = _get_row(session, channel, external_user_id)
        if row is not None:
            row.pending_json = "{}"
            session.commit()


def match_pending(channel: str, external_user_id: str, text: str) -> Optional[dict[str, Any]]:
    """Map ``1`` / ``2`` / an option label to the stored option value."""
    pending = get_pending(channel, external_user_id)
    options = pending.get("options") or []
    t = (text or "").strip().rstrip("。.!！")
    if not options or not t:
        return None
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(options):
            return dict(options[idx].get("value") or {})
        return None
    for opt in options:
        if t == str(opt.get("label") or "").strip():
            return dict(opt.get("value") or {})
    return None
