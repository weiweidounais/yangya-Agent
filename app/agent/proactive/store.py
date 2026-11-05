"""Pending proactive items + idempotent run ledger (SQLite)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.data_access.schema import ensure_tables
from app.db import Base, init_db
from app.data_access.session import SessionLocal


class ProactiveItem(Base):
    """User-facing proactive item (reminder / schedule output) stored for the in-app inbox."""

    __tablename__ = "proactive_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True, default="reminder")
    title: Mapped[str] = mapped_column(String(256), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # pending | delivered | dismissed | consumed
    delivered_via: Mapped[str] = mapped_column(String(64), default="")
    period_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ProactiveRun(Base):
    """Idempotency ledger: one successful run per (user, job, period)."""

    __tablename__ = "proactive_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "job_type", "period_key", name="uq_proactive_run"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    period_key: Mapped[str] = mapped_column(String(128), default="")
    ok: Mapped[int] = mapped_column(Integer, default=1)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


def _ensure() -> None:
    from app.db import ENGINE

    init_db()
    ensure_tables(ENGINE)


def _item_dict(row: ProactiveItem) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": row.id,
        "user_id": row.user_id,
        "kind": row.kind,
        "title": row.title or "",
        "body": row.body or "",
        "payload": payload,
        "status": row.status or "pending",
        "delivered_via": row.delivered_via or "",
        "period_key": row.period_key or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def has_run(user_id: str, job_type: str, period_key: str) -> bool:
    _ensure()
    uid = str(user_id or "").strip()
    if not uid or not job_type or not period_key:
        return False
    with SessionLocal() as session:
        row = session.execute(
            select(ProactiveRun).where(
                ProactiveRun.user_id == uid,
                ProactiveRun.job_type == str(job_type),
                ProactiveRun.period_key == str(period_key),
            )
        ).scalar_one_or_none()
        return row is not None


def record_run(
    user_id: str,
    job_type: str,
    period_key: str,
    *,
    ok: bool = True,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    _ensure()
    uid = str(user_id or "").strip()
    if not uid:
        return {}
    with SessionLocal() as session:
        existing = session.execute(
            select(ProactiveRun).where(
                ProactiveRun.user_id == uid,
                ProactiveRun.job_type == str(job_type),
                ProactiveRun.period_key == str(period_key),
            )
        ).scalar_one_or_none()
        if existing:
            return {
                "id": existing.id,
                "user_id": existing.user_id,
                "job_type": existing.job_type,
                "period_key": existing.period_key,
                "ok": bool(existing.ok),
                "duplicate": True,
            }
        row = ProactiveRun(
            user_id=uid,
            job_type=str(job_type),
            period_key=str(period_key),
            ok=1 if ok else 0,
            detail_json=json.dumps(detail or {}, ensure_ascii=False)[:4000],
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "id": row.id,
            "user_id": row.user_id,
            "job_type": row.job_type,
            "period_key": row.period_key,
            "ok": bool(row.ok),
            "duplicate": False,
        }


def create_item(
    user_id: str,
    kind: str,
    *,
    title: str = "",
    body: str = "",
    payload: Optional[dict[str, Any]] = None,
    period_key: str = "",
    status: str = "pending",
    delivered_via: str = "",
) -> dict[str, Any]:
    _ensure()
    uid = str(user_id or "").strip()
    if not uid:
        return {}
    with SessionLocal() as session:
        row = ProactiveItem(
            user_id=uid,
            kind=(kind or "reminder").strip() or "reminder",
            title=(title or "")[:256],
            body=(body or "")[:8000],
            payload_json=json.dumps(payload or {}, ensure_ascii=False)[:8000],
            status=(status or "pending").strip() or "pending",
            delivered_via=(delivered_via or "")[:64],
            period_key=str(period_key or "")[:128],
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _item_dict(row)


def list_pending(
    user_id: Optional[str] = None,
    *,
    limit: int = 50,
    status: Optional[str] = "pending",
) -> list[dict[str, Any]]:
    _ensure()
    limit = max(1, min(int(limit or 50), 200))
    with SessionLocal() as session:
        q = select(ProactiveItem)
        if user_id:
            q = q.where(ProactiveItem.user_id == str(user_id))
        if status is not None and str(status) != "":
            q = q.where(ProactiveItem.status == str(status))
        q = q.order_by(ProactiveItem.created_at.desc()).limit(limit)
        rows = list(session.execute(q).scalars().all())
    return [_item_dict(r) for r in rows]


def mark_item(
    item_id: int,
    *,
    status: str,
    delivered_via: str = "",
) -> Optional[dict[str, Any]]:
    _ensure()
    with SessionLocal() as session:
        row = session.get(ProactiveItem, int(item_id))
        if not row:
            return None
        row.status = str(status or row.status)
        if delivered_via:
            row.delivered_via = delivered_via[:64]
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        return _item_dict(row)


def last_run_at(job_type: str, user_id: Optional[str] = None) -> Optional[datetime]:
    _ensure()
    with SessionLocal() as session:
        q = select(ProactiveRun).where(ProactiveRun.job_type == str(job_type))
        if user_id:
            q = q.where(ProactiveRun.user_id == str(user_id))
        q = q.order_by(ProactiveRun.created_at.desc()).limit(1)
        row = session.execute(q).scalar_one_or_none()
        return row.created_at if row else None


def item_owner(item_id: int) -> Optional[str]:
    """Owner user_id of a proactive item (None if missing)."""
    _ensure()
    with SessionLocal() as session:
        row = session.get(ProactiveItem, int(item_id))
        return str(row.user_id) if row is not None else None
