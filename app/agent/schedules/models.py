"""Private schedule state and durable execution/confirmation ledgers."""
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


def now():
    return datetime.now(timezone.utc)


class UserActivity(Base):
    """Authenticated activity, separate from background profile writes."""
    __tablename__ = "user_activity"
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Schedule(Base):
    __tablename__ = "user_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    job_kind: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger_json: Mapped[str] = mapped_column(Text)
    tz: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    prompt_text: Mapped[str] = mapped_column(Text, default="")
    delivery: Mapped[str] = mapped_column(String(32), default="both")
    channel_preference: Mapped[str] = mapped_column(String(32), default="auto")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(32), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    max_runs: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class ScheduleRun(Base):
    __tablename__ = "user_schedule_runs"
    __table_args__ = (UniqueConstraint("schedule_id", "period_key", name="uq_user_schedule_period"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    period_key: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    item_id: Mapped[int | None] = mapped_column(Integer)  # logical FK to proactive_items


class ScheduleProposal(Base):
    __tablename__ = "user_schedule_proposals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)
    preview_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
