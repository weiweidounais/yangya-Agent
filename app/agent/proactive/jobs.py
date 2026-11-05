"""Proactive job runners: user schedules (reminders, custom tasks) on a tick.

Never bypasses confirm cards / never silently sends anything externally.
Deliver via the existing outbound adapters when a channel is bound.
"""

from __future__ import annotations

from app.agent.tool_registry import tool

import logging
import time
from typing import Any, Optional

from app.agent.proactive import store
from app.agent.proactive.config import ProactiveConfig, get_config

logger = logging.getLogger("app.agent.proactive")


def list_candidate_user_ids(limit: int = 50) -> list[str]:
    """Users with a profile and/or episodic activity."""
    from app import db
    from app.agent.memory.episodic import EpisodicEvent
    from app.data_access.session import SessionLocal
    from sqlalchemy import select

    uids: list[str] = []
    seen: set[str] = set()

    def _add(uid: str) -> None:
        u = str(uid or "").strip()
        if u and u not in seen:
            seen.add(u)
            uids.append(u)

    try:
        for uid in db.list_user_ids():
            _add(uid)
            if len(uids) >= limit:
                return uids[:limit]
    except Exception:
        pass

    try:
        from app.db import ENGINE

        db.init_db()
        from app.data_access.schema import ensure_tables
        ensure_tables(ENGINE)
        with SessionLocal() as session:
            rows = session.execute(
                select(EpisodicEvent.user_id).distinct().limit(limit * 2)
            ).scalars().all()
            for uid in rows:
                _add(uid)
                if len(uids) >= limit:
                    return uids[:limit]
    except Exception:
        pass

    return uids[:limit]


def _record_trace(
    *,
    user_id: str,
    job_type: str,
    ok: bool,
    latency_ms: float,
    detail: Optional[dict[str, Any]] = None,
    error: str = "",
) -> None:
    from app.agent.harness.traces import record_step

    tid = f"proactive:{user_id}:{job_type}"
    record_step(
        thread_id=tid,
        user_id=user_id,
        step=0,
        intent="proactive",
        tool=job_type,
        model="",
        latency_ms=latency_ms,
        ok=ok,
        error=error,
        detail={"kind": "proactive", "job": job_type, **(detail or {})},
    )


def _try_deliver(user_id: str, text: str, *, kind: str, channel_preference: str = "auto") -> dict[str, Any]:
    """Deliver via first channel binding if any; never sends draft/confirm payloads."""
    from app.channels.binding import list_bindings

    bindings = list_bindings(user_id=user_id)
    if channel_preference != "auto":
        allowed = {"feishu"} if channel_preference == "feishu" else {"wechat", "openclaw_wechat", "openclaw-weixin"}
        bindings = [b for b in bindings if b.get("channel") in allowed]
    if not bindings:
        return {"ok": False, "skipped": True, "reason": "no_binding"}
    # Prefer feishu, then wechat
    order = {"feishu": 0, "openclaw_wechat": 1, "openclaw-weixin": 1, "wechat": 2}
    bindings = sorted(bindings, key=lambda b: order.get(b.get("channel") or "", 9))
    b = bindings[0]
    channel = b.get("channel") or ""
    external = b.get("external_user_id") or ""
    # Soft text only — no confirm card, no private documents
    safe_text = (text or "")[:1500]
    if not safe_text:
        return {"ok": False, "skipped": True, "reason": "empty_text"}
    result = _deliver_self(
        user_id=user_id,
        channel=channel,
        external_user_id=external,
        text=f"【主动提醒·{kind}】\n{safe_text}",
        card=None,
    )
    result = dict(result or {})
    result["channel"] = channel
    result["external_user_id"] = external
    return result


@tool("proactive.deliver", risk_level="notify_self", description="Deliver a reminder to the owner's connected channel")
def _deliver_self(user_id: str, channel: str, external_user_id: str, text: str, card: Any = None) -> dict[str, Any]:
    from app.channels.outbound import deliver_channel_outbound
    return deliver_channel_outbound(channel=channel, external_user_id=external_user_id, text=text, card=card)


def run_tick(cfg: Optional[ProactiveConfig] = None) -> dict[str, Any]:
    """One scheduler tick: run due user schedules within soft budgets."""
    cfg = cfg or get_config()
    if not cfg.enabled:
        return {"ok": True, "skipped": True, "reason": "disabled"}

    started = time.perf_counter()
    users = list_candidate_user_ids(limit=cfg.max_users_per_tick)
    out: dict[str, Any] = {"ok": True, "users": len(users)}

    from app.agent.schedules import runner as schedules
    out["schedules"] = schedules.run_due(cfg, started=started)
    if (time.perf_counter() - started) >= cfg.soft_budget_seconds:
        out["budget_stop"] = "schedules"
    else:
        from app.companion import report as companion_report

        out["weekly_reports"] = companion_report.run_weekly_reports(cfg)
        if (time.perf_counter() - started) >= cfg.soft_budget_seconds:
            out["budget_stop"] = "weekly_reports"

    out["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    return out
