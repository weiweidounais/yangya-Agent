"""Per-user spend ledger with daily / monthly ¥ caps.

Spend is the gateway token estimate × ``AGENT_COST_PER_1K_TOKENS`` (¥ per 1k
tokens), recorded per user per local day. Caps depend on the user's plan:

  AGENT_COST_CAP_FREE_DAILY   (¥2)    AGENT_COST_CAP_FREE_MONTHLY  (¥30)
  AGENT_COST_CAP_PAID_DAILY   (¥20)   AGENT_COST_CAP_PAID_MONTHLY  (¥300)

A cap ≤ 0 disables it. Plan: ``set_plan`` row, else ``AGENT_PAID_USERS``
(comma-separated user ids), else free. Days roll over at local midnight in
``AGENT_COST_TZ`` (default Asia/Shanghai).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Float, String, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.data_access.session import SessionLocal
from app.audit_db import bind_audit_table, ensure_audit_table

DEFAULT_PRICE_PER_1K = 0.012
FREE, PAID = "free", "paid"
_CAP_DEFAULTS = {(FREE, "daily"): 2.0, (FREE, "monthly"): 30.0, (PAID, "daily"): 20.0, (PAID, "monthly"): 300.0}


class CostSpend(Base):
    __tablename__ = "agent_cost_spend"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD local
    cny: Mapped[float] = mapped_column(Float, default=0.0)


class UserPlan(Base):
    __tablename__ = "agent_user_plans"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan: Mapped[str] = mapped_column(String(16), default=FREE)


bind_audit_table(CostSpend.__table__)

# Plans remain an operational store, outside the Postgres private business DB.
from app.db import ENGINE
if ENGINE.dialect.name == "postgresql":
    bind_audit_table(UserPlan.__table__)

_ready = False


def _ensure() -> None:
    global _ready
    if not _ready:
        from app.db import ENGINE, _memory_session

        session = _memory_session.get()
        ensure_audit_table(CostSpend.__table__)
        if ENGINE.dialect.name == "postgresql":
            ensure_audit_table(UserPlan.__table__)
        else:
            Base.metadata.create_all(session.connection() if session is not None else ENGINE, tables=[UserPlan.__table__])
        _ready = True


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def price_per_1k_tokens() -> float:
    return max(0.0, _float_env("AGENT_COST_PER_1K_TOKENS", DEFAULT_PRICE_PER_1K))


def cny_for_tokens(tokens: int) -> float:
    return round(max(0, int(tokens)) / 1000.0 * price_per_1k_tokens(), 6)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo((os.getenv("AGENT_COST_TZ") or "Asia/Shanghai").strip())
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _tz_label() -> str:
    key = _tz().key
    return "北京时间" if key in ("Asia/Shanghai", "Asia/Chongqing", "PRC") else key


def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(_tz())).astimezone(_tz())


def plan_for(user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        return FREE
    _ensure()
    with SessionLocal() as s:
        row = s.get(UserPlan, uid)
        if row and row.plan in (FREE, PAID):
            return row.plan
    paid = {u.strip() for u in (os.getenv("AGENT_PAID_USERS") or "").split(",") if u.strip()}
    return PAID if uid in paid else FREE


def set_plan(user_id: str, plan: str) -> None:
    _ensure()
    with SessionLocal() as s:
        row = s.get(UserPlan, user_id)
        if row is None:
            s.add(UserPlan(user_id=user_id, plan=plan))
        else:
            row.plan = plan
        s.commit()


def caps_for(plan: str) -> dict[str, float]:
    p = PAID if plan == PAID else FREE
    return {
        scope: _float_env(f"AGENT_COST_CAP_{p.upper()}_{scope.upper()}", _CAP_DEFAULTS[(p, scope)])
        for scope in ("daily", "monthly")
    }


def record_spend(user_id: str, cny: float, *, now: Optional[datetime] = None) -> None:
    uid = str(user_id or "").strip()
    if not uid or cny <= 0:
        return
    _ensure()
    day = _now(now).strftime("%Y-%m-%d")
    with SessionLocal() as s:
        stmt = insert(CostSpend).values(user_id=uid, day=day, cny=float(cny))
        s.execute(stmt.on_conflict_do_update(
            index_elements=[CostSpend.user_id, CostSpend.day],
            set_={"cny": CostSpend.cny + stmt.excluded.cny},
        ))
        s.commit()


def usage(user_id: str, *, now: Optional[datetime] = None) -> dict[str, float]:
    uid = str(user_id or "").strip()
    if not uid:
        return {"today": 0.0, "month": 0.0}
    _ensure()
    n = _now(now)
    day, month = n.strftime("%Y-%m-%d"), n.strftime("%Y-%m")
    with SessionLocal() as s:
        today = s.execute(
            select(func.coalesce(func.sum(CostSpend.cny), 0.0)).where(
                CostSpend.user_id == uid, CostSpend.day == day
            )
        ).scalar_one()
        mon = s.execute(
            select(func.coalesce(func.sum(CostSpend.cny), 0.0)).where(
                CostSpend.user_id == uid, CostSpend.day.like(f"{month}-%")
            )
        ).scalar_one()
    return {"today": round(float(today), 4), "month": round(float(mon), 4)}


def _reset_at(scope: str, n: datetime) -> datetime:
    midnight = n.replace(hour=0, minute=0, second=0, microsecond=0)
    if scope == "daily":
        return midnight + timedelta(days=1)
    first = midnight.replace(day=1)
    return (first + timedelta(days=32)).replace(day=1)


@dataclass
class CapStatus:
    ok: bool
    plan: str
    today: float
    month: float
    daily_cap: float
    monthly_cap: float
    scope: Optional[str] = None  # which cap is hit
    reset_at: Optional[str] = None  # ISO, local tz
    reset_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def check(user_id: str, *, extra: float = 0.0, now: Optional[datetime] = None) -> CapStatus:
    """Is the user still under both caps (counting ``extra`` not yet recorded)?"""
    n = _now(now)
    plan = plan_for(user_id)
    caps = caps_for(plan)
    use = usage(user_id, now=n)
    today, month = use["today"] + max(0.0, extra), use["month"] + max(0.0, extra)
    st = CapStatus(True, plan, round(today, 4), round(month, 4), caps["daily"], caps["monthly"])
    if not str(user_id or "").strip():
        return st
    hit = None
    if caps["monthly"] > 0 and month >= caps["monthly"]:
        hit = "monthly"
    elif caps["daily"] > 0 and today >= caps["daily"]:
        hit = "daily"
    if hit:
        reset = _reset_at(hit, n)
        st.ok, st.scope, st.reset_at = False, hit, reset.isoformat()
        st.reset_text = (
            reset.strftime("明天 %H:%M") if hit == "daily" else reset.strftime("%m月%d日 %H:%M")
        )
    return st


def cap_message(st: CapStatus) -> str:
    label = "今天" if st.scope == "daily" else "这个月"
    cap = st.daily_cap if st.scope == "daily" else st.monthly_cap
    plan = "免费版" if st.plan == FREE else "付费版"
    return (
        f"{label}的用量额度到了（{plan}上限 ¥{cap:g}），我先停在这里。"
        f"额度会在 {st.reset_text}（{_tz_label()}）恢复；进度已经保存，恢复后点「继续」我从这里接着做。"
    )
