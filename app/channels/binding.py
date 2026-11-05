"""Channel ↔ user_id binding (one account across web and channels)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, init_db
from app.data_access.session import SessionLocal


class ChannelBinding(Base):
    __tablename__ = "channel_bindings"
    __table_args__ = (
        UniqueConstraint("channel", "external_user_id", name="uq_channel_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(64), index=True)
    external_user_id: Mapped[str] = mapped_column(String(256), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


def _ensure() -> None:
    init_db()


def bind_channel(
    channel: str,
    external_user_id: str,
    user_id: str,
    *,
    display_name: str = "",
) -> dict[str, Any]:
    """Upsert binding. Same external id always maps to one user."""
    _ensure()
    channel = (channel or "").strip()
    external_user_id = str(external_user_id or "").strip()
    user_id = str(user_id or "").strip()
    if not channel or not external_user_id or not user_id:
        raise ValueError("channel, external_user_id, user_id required")
    from app.channels.settings import is_reserved_user_id

    if is_reserved_user_id(user_id):
        raise ValueError(f"reserved user_id {user_id!r} cannot be bound to a channel")
    from app.data_access.channel_identity import rebind_channel_identity
    return rebind_channel_identity(channel, external_user_id, user_id,
                                   display_name=display_name)


def get_binding(channel: str, external_user_id: str) -> Optional[dict[str, Any]]:
    _ensure()
    with SessionLocal() as session:
        row = session.execute(
            select(ChannelBinding).where(
                ChannelBinding.channel == channel,
                ChannelBinding.external_user_id == str(external_user_id),
            )
        ).scalar_one_or_none()
        if not row:
            return None
        return {
            "channel": row.channel,
            "external_user_id": row.external_user_id,
            "user_id": row.user_id,
            "display_name": row.display_name,
        }


def resolve_user_id(channel: str, external_user_id: str) -> Optional[str]:
    b = get_binding(channel, external_user_id)
    return b["user_id"] if b else None


def list_bindings(user_id: Optional[str] = None) -> list[dict[str, Any]]:
    _ensure()
    with SessionLocal() as session:
        q = select(ChannelBinding)
        if user_id:
            q = q.where(ChannelBinding.user_id == user_id)
        rows = session.execute(q).scalars().all()
        return [
            {
                "channel": r.channel,
                "external_user_id": r.external_user_id,
                "user_id": r.user_id,
                "display_name": r.display_name,
            }
            for r in rows
        ]


def unbind_channel(channel: str, external_user_id: str) -> bool:
    _ensure()
    with SessionLocal() as session:
        row = session.execute(
            select(ChannelBinding).where(
                ChannelBinding.channel == channel,
                ChannelBinding.external_user_id == str(external_user_id),
            )
        ).scalar_one_or_none()
        if not row:
            return False
        session.delete(row)
        session.commit()
        return True


def _parse_open_id_entry(entry: str) -> tuple[str, str]:
    """Parse ``open_id`` or ``open_id:display_name``."""
    entry = (entry or "").strip()
    if not entry:
        return "", ""
    if ":" in entry:
        oid, name = entry.split(":", 1)
        return oid.strip(), name.strip()
    return entry, ""


def parse_auto_bind_specs() -> list[dict[str, str]]:
    """Collect binding specs from env (no secrets).

    Supported:
      FEISHU_BIND_USER_ID + FEISHU_BIND_OPEN_IDS=ou_a:名,ou_b:名
      CHANNEL_AUTO_BIND=feishu:ou_x:uuid:名|openclaw_wechat:wx:uuid
    """
    import os

    specs: list[dict[str, str]] = []
    uid = (os.getenv("FEISHU_BIND_USER_ID") or "").strip()
    feishu_ids = (os.getenv("FEISHU_BIND_OPEN_IDS") or "").strip()
    if uid and feishu_ids:
        for part in feishu_ids.split(","):
            oid, name = _parse_open_id_entry(part)
            if oid:
                specs.append(
                    {
                        "channel": "feishu",
                        "external_user_id": oid,
                        "user_id": uid,
                        "display_name": name,
                    }
                )

    raw = (os.getenv("CHANNEL_AUTO_BIND") or "").strip()
    if raw:
        for chunk in raw.replace("\n", "|").split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            bits = chunk.split(":")
            if len(bits) < 3:
                continue
            channel = bits[0].strip()
            external = bits[1].strip()
            target_uid = bits[2].strip()
            display = ":".join(bits[3:]).strip() if len(bits) > 3 else ""
            if channel and external and target_uid:
                specs.append(
                    {
                        "channel": channel,
                        "external_user_id": external,
                        "user_id": target_uid,
                        "display_name": display,
                    }
                )
    return specs


from app.data_access import system_scope


@system_scope()
def apply_auto_bindings() -> list[dict[str, Any]]:
    """Upsert all env-driven channel bindings. Safe to call on every startup."""
    import logging

    applied: list[dict[str, Any]] = []
    for spec in parse_auto_bind_specs():
        try:
            row = bind_channel(
                spec["channel"],
                spec["external_user_id"],
                spec["user_id"],
                display_name=spec.get("display_name") or "",
            )
        except ValueError as exc:
            logging.getLogger("channels.binding").warning("auto-bind skipped: %s", exc)
            continue
        applied.append(row)
    return applied
