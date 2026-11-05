"""每周搭子周报：生成文本、写入应用内收件箱、复用已绑定渠道推送。

不新增表：复用 proactive_items 与 proactive_runs 做收件箱与幂等账本。
推送只发文本，不发草稿、凭证或图片。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent.proactive import store as proactive_store
from app.agent.proactive.config import ProactiveConfig
from app.companion import store as companion_store

logger = logging.getLogger("app.companion.report")

JOB_TYPE = "companion_weekly"


def _tz_for(profile: dict[str, Any]) -> ZoneInfo:
    name = str(profile.get("tz") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def period_key(now: datetime, tz: ZoneInfo) -> str:
    local = now.astimezone(tz)
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def build_report(user_id: str, *, days: int = 7, now: Optional[datetime] = None) -> dict[str, Any]:
    """Text report from the same numbers the panel shows (no new math)."""
    moment = now or datetime.now(timezone.utc)
    recap = companion_store.recap(user_id, days=days, now=moment)
    state = companion_store.status(user_id, now=moment)
    body = state.get("body") or {}
    energy = state.get("energy") or {}
    social = state.get("social") or {}
    lines = [recap["summary"]]
    if energy:
        lines.append(
            f"今天摄入 {int(energy.get('intake') or 0)} / 维持约 {int(energy.get('maintenance') or 0)} kcal，"
            f"运动消耗 {int(energy.get('exercise_burn') or 0)} kcal。"
        )
    if state.get("streak_days"):
        lines.append(f"当前连续训练 {state['streak_days']} 天。")
    if state.get("dead"):
        lines.append(f"{state.get('name')} 已经饿睡，回来说一声就能唤醒新一代。")
    lines.append(_suggestion(recap, state, body))
    return {
        "title": f"{state.get('name') or '阿动'}的本周周报",
        "body": "\n".join(line for line in lines if line),
        "recap": recap,
        "state": {k: state.get(k) for k in ("name", "generation", "dead", "streak_days", "hunger_days_left")},
        "social": social.get("label") or "",
        "period": period_key(moment, _tz_for(body)),
    }


def _suggestion(recap: dict[str, Any], state: dict[str, Any], body: dict[str, Any]) -> str:
    sessions = int(recap.get("sessions") or 0)
    target = int(body.get("sport_days_per_week") or 0) or 3
    if state.get("dead"):
        return "下次回来不用补练，从 20 分钟开始就好。"
    if sessions == 0:
        return f"这周没练也没关系；下周先把目标降到 {max(1, target - 1)} 次 × 20 分钟，能完成比练狠重要。"
    if sessions >= target:
        return f"这周完成 {sessions}/{target} 次，超了；下周别再往上加，留一天完全休息。"
    if recap.get("weight_delta_kg", 0) <= -1 and body.get("body_goal") == "cut":
        return "体重掉得偏快，下周把每天摄入加 200 kcal，优先补蛋白，别硬扛。"
    return f"这周 {sessions}/{target} 次；下周把缺的那次补在一个固定时间，比随机找时间更容易做到。"


def weekly_window(now: datetime, tz: ZoneInfo) -> tuple[int, int]:
    """(weekday, hour) in the user's timezone; Monday=0."""
    local = now.astimezone(tz)
    return local.weekday(), local.hour


def should_send(now: datetime, tz: ZoneInfo, *, weekday: int, hour: int) -> bool:
    current = weekly_window(now, tz)
    return current == (weekday, hour)


def deliver_report(user_id: str, report: dict[str, Any], *, now: Optional[datetime] = None,
                   channel_preference: str = "auto") -> dict[str, Any]:
    """Create the in-app item and push text to the first bound channel."""
    from app.agent.proactive import jobs

    moment = now or datetime.now(timezone.utc)
    period = report.get("period") or ""
    item = proactive_store.create_item(
        user_id,
        "companion_weekly",
        title=report.get("title") or "本周周报",
        body=report.get("body") or "",
        payload={"period": period, "recap": report.get("recap"), "state": report.get("state")},
        period_key=period,
        status="delivered",
        delivered_via="in_app",
    )
    push = jobs._try_deliver(user_id, report.get("body") or "", kind="周报",
                             channel_preference=channel_preference)
    if push.get("ok"):
        proactive_store.mark_item(item.get("id"), status="delivered", delivered_via=push.get("channel") or "in_app")
    return {"item": item, "delivery": push, "period": period}


def run_weekly_reports(cfg: Optional[ProactiveConfig] = None, *,
                       now: Optional[datetime] = None, limit: int = 200) -> dict[str, Any]:
    """Scheduler entry: send at most one report per user per ISO week."""
    import os

    if os.getenv("COMPANION_WEEKLY_ENABLED", "1").strip().lower() in {"0", "false", "off", "no"}:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    try:
        weekday = int(os.getenv("COMPANION_WEEKLY_WEEKDAY", "6"))
        hour = int(os.getenv("COMPANION_WEEKLY_HOUR", "20"))
    except ValueError:
        weekday, hour = 6, 20
    moment = now or datetime.now(timezone.utc)
    from app.agent.proactive.jobs import list_candidate_user_ids
    from app.data_access import user_scope

    users = list_candidate_user_ids(limit=limit)
    sent, skipped = 0, 0
    details = []
    for uid in users:
        try:
            with user_scope(uid):
                profile = companion_store.db.get_profile(uid) or {}
                if not profile.get("body_height_cm") or not profile.get("body_weight_kg"):
                    skipped += 1
                    continue
                tz = _tz_for(profile)
                if not should_send(moment, tz, weekday=weekday, hour=hour):
                    skipped += 1
                    continue
                period = period_key(moment, tz)
                if proactive_store.has_run(uid, JOB_TYPE, period):
                    skipped += 1
                    continue
                report = build_report(uid, now=moment)
                delivered = deliver_report(uid, report, now=moment)
                proactive_store.record_run(uid, JOB_TYPE, period, ok=True,
                                           detail={"channel": (delivered.get("delivery") or {}).get("channel") or "in_app"})
                sent += 1
                details.append({"user_id": uid, "period": period,
                                "delivered": (delivered.get("delivery") or {}).get("channel") or "in_app"})
        except Exception as exc:  # one broken user must not stop the rest
            logger.warning("weekly report failed: %s", type(exc).__name__)
            skipped += 1
    return {"ok": True, "sent": sent, "skipped": skipped, "users": len(users), "details": details[:20]}
