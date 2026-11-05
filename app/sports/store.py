"""运动搭子领域层：运动档案、训练记录、周计划。

数据全部落在既有私有存储里，不新增表：

* 运动档案 → 用户 profile JSON（因此记忆卡可直接编辑）
* 训练记录 → episodic_events（``kind="workout"``，可检索、可删除）
* 周计划   → 用户 profile JSON 的 ``sport_plan`` 文档
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app import db
from app.agent.memory import episodic
from app.tools.prompt_data import data_block

SCALAR_FIELDS = (
    "sport_goal",
    "sport_level",
    "sport_days_per_week",
    "sport_session_minutes",
    "sport_injuries",
)
LIST_FIELDS = ("sport_equipment",)
PLAN_KEY = "sport_plan"

SPORTS = ("跑步", "骑行", "游泳", "力量训练", "球类", "瑜伽", "徒步", "其他")
INTENSITIES = ("轻松", "中等", "吃力")

PROFILE_LABELS = {
    "sport_goal": "运动目标",
    "sport_level": "训练水平",
    "sport_days_per_week": "每周训练天数",
    "sport_session_minutes": "单次时长（分钟）",
    "sport_injuries": "伤病与限制",
    "sport_equipment": "器械与场地",
}


def _uid(user_id: str) -> str:
    """Resolve the caller from the trusted scope; refuse a mismatched id.

    ``profile_read`` is itself a registry tool, so the data layer already
    filters by the active identity. Catching a mismatch here fails closed
    instead of silently reading the scoped user.
    """
    from app.data_access import IdentityRequired, NotFound, current_user

    try:
        scoped = current_user()
    except IdentityRequired:
        scoped = None
    uid = str(user_id or "").strip()
    if not uid:
        if scoped:
            return scoped
        raise ValueError("需要当前用户")
    if scoped is not None and uid != scoped:
        raise NotFound("这条数据不属于当前账号")
    return uid


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any, low: int, high: int) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _float(value: Any, low: float, high: float) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if low <= number <= high else None


def get_profile(user_id: str) -> dict[str, Any]:
    """Only the sport fields of the saved profile (never the whole profile)."""
    stored = db.get_profile(_uid(user_id)) or {}
    out: dict[str, Any] = {}
    for key in SCALAR_FIELDS:
        value = stored.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    for key in LIST_FIELDS:
        value = stored.get(key)
        if isinstance(value, list) and value:
            out[key] = [str(v)[:80] for v in value[:20]]
        elif isinstance(value, str) and value.strip():
            out[key] = [value.strip()[:80]]
    return out


def update_profile(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge sport fields into the profile; empty values never erase."""
    uid = _uid(user_id)
    clean: dict[str, Any] = {}
    for key in SCALAR_FIELDS:
        if key not in (patch or {}):
            continue
        if key == "sport_days_per_week":
            value = _int(patch[key], 1, 7)
        elif key == "sport_session_minutes":
            value = _int(patch[key], 10, 300)
        else:
            value = _clean(patch[key])
        if value not in (None, ""):
            clean[key] = value
    equipment = patch.get("sport_equipment") if isinstance(patch, dict) else None
    if isinstance(equipment, str):
        equipment = [part.strip() for part in equipment.replace("、", ",").split(",") if part.strip()]
    if isinstance(equipment, list) and equipment:
        clean["sport_equipment"] = [str(item).strip()[:80] for item in equipment[:20] if str(item).strip()]
    if not clean:
        raise ValueError("没有可保存的运动档案字段")
    merged = db.merge_and_save(uid, clean)
    changed = [PROFILE_LABELS.get(key, key) for key in clean]
    episodic.write_event(uid, "profile_updated",
                         "运动档案更新：" + "、".join(changed),
                         tags=["sport", "profile"], payload={"fields": list(clean)})
    return {"ok": True, "updated": changed, "profile": get_profile(uid), "memory_rev": merged.get("memory_rev")}


def log_workout(user_id: str, *, sport: str, duration_min: int,
                distance_km: Optional[float] = None, intensity: str = "",
                note: str = "", performed_on: str = "") -> dict[str, Any]:
    uid = _uid(user_id)
    sport_name = _clean(sport, 24) or "其他"
    duration = _int(duration_min, 1, 600)
    if duration is None:
        raise ValueError("训练时长需要在 1–600 分钟之间")
    distance = _float(distance_km, 0, 500) if distance_km not in (None, "") else None
    level = _clean(intensity, 12)
    day = _clean(performed_on, 10)
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            day = ""
    summary = f"{sport_name} {duration} 分钟"
    if distance:
        summary += f" · {distance:g} 公里"
    if level:
        summary += f" · {level}"
    event = episodic.write_event(uid, "workout", summary,
                                tags=["workout", sport_name],
                                payload={"sport": sport_name, "duration_min": duration,
                                         "distance_km": distance, "intensity": level,
                                         "note": _clean(note, 500), "performed_on": day})
    if not event:
        raise ValueError("训练记录没有保存成功，请稍后重试")
    return {"ok": True, "session": event, "stats": stats(uid, days=7)}


def list_workouts(user_id: str, *, days: int = 28, limit: int = 200) -> list[dict[str, Any]]:
    uid = _uid(user_id)
    days = max(1, min(int(days or 28), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = episodic.list_events(uid, kind="workout", since=since, limit=limit)
    return [{"id": e["id"], "summary": e["summary"], "payload": e.get("payload") or {},
             "created_at": e["created_at"]} for e in events]


def _streak(days: set[date]) -> int:
    today = datetime.now(timezone.utc).date()
    cursor = today if today in days else today - timedelta(days=1)
    if cursor not in days:
        return 0
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def stats(user_id: str, *, days: int = 28) -> dict[str, Any]:
    sessions = list_workouts(user_id, days=days)
    total_minutes = 0
    total_km = 0.0
    by_sport: dict[str, int] = {}
    trained: set[date] = set()
    for session in sessions:
        payload = session.get("payload") or {}
        total_minutes += int(payload.get("duration_min") or 0)
        total_km += float(payload.get("distance_km") or 0)
        name = str(payload.get("sport") or "其他")
        by_sport[name] = by_sport.get(name, 0) + 1
        try:
            trained.add(datetime.fromisoformat(str(session["created_at"]).replace("Z", "+00:00")).date())
        except (ValueError, TypeError, AttributeError):
            pass
    return {
        "days": days,
        "sessions": len(sessions),
        "minutes": total_minutes,
        "distance_km": round(total_km, 2),
        "by_sport": by_sport,
        "days_trained": len(trained),
        "streak_days": _streak(trained),
        "recent": sessions[:5],
    }


def save_plan(user_id: str, *, goal: str, items: Optional[list[dict[str, Any]]] = None,
              level: str = "", weeks: int = 4, days_per_week: int = 3,
              session_minutes: int = 40, focus: str = "", notes: str = "") -> dict[str, Any]:
    uid = _uid(user_id)
    week = max(1, min(int(weeks or 4), 52))
    per_week = _int(days_per_week, 1, 7) or 3
    minutes = _int(session_minutes, 10, 300) or 40
    cleaned_items: list[dict[str, Any]] = []
    for item in (items or [])[:14]:
        if not isinstance(item, dict):
            continue
        entry = {
            "day": _clean(item.get("day"), 12),
            "focus": _clean(item.get("focus"), 40),
            "duration_min": _int(item.get("duration_min"), 5, 300) or minutes,
            "intensity": _clean(item.get("intensity"), 12),
        }
        if entry["focus"]:
            cleaned_items.append(entry)
    plan = {
        "goal": _clean(goal, 120),
        "level": _clean(level, 40),
        "weeks": week,
        "days_per_week": per_week,
        "session_minutes": minutes,
        "focus": _clean(focus, 120),
        "notes": _clean(notes, 500),
        "items": cleaned_items,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not plan["goal"]:
        raise ValueError("计划需要先写清目标")
    db.merge_and_save(uid, {PLAN_KEY: plan})
    episodic.write_event(uid, "sport_plan",
                         f"保存训练计划：{plan['goal']} · 每周 {per_week} 练 · {week} 周",
                         tags=["sport", "plan"], payload={"goal": plan["goal"], "weeks": week})
    return {"ok": True, "plan": plan}


def get_plan(user_id: str) -> dict[str, Any]:
    stored = db.get_profile(_uid(user_id)) or {}
    plan = stored.get(PLAN_KEY)
    return plan if isinstance(plan, dict) else {}


def plan_text(plan: dict[str, Any]) -> str:
    if not plan:
        return "还没有保存训练计划。告诉我目标和每周能练几天，我来排。"
    lines = [
        f"目标：{plan.get('goal') or '未填写'}",
        f"周期：{plan.get('weeks')} 周 · 每周 {plan.get('days_per_week')} 练 · 单次约 {plan.get('session_minutes')} 分钟",
    ]
    if plan.get("level"):
        lines.append(f"水平：{plan['level']}")
    if plan.get("focus"):
        lines.append(f"重点：{plan['focus']}")
    for item in plan.get("items") or []:
        detail = item.get("focus") or ""
        extra = []
        if item.get("duration_min"):
            extra.append(f"{item['duration_min']} 分钟")
        if item.get("intensity"):
            extra.append(str(item["intensity"]))
        lines.append(f"- {item.get('day') or '某天'}：{detail}" + (f"（{' · '.join(extra)}）" if extra else ""))
    if plan.get("notes"):
        lines.append(f"备注：{plan['notes']}")
    return "\n".join(lines)


def prompt_block(user_id: str) -> str:
    """Bounded trusted snapshot for the chat prompt (wrapped as data, not rules)."""
    from app.data_access import IdentityRequired, current_user

    try:
        scoped = current_user()
    except IdentityRequired:
        scoped = None
    uid = scoped or str(user_id or "").strip()
    if not uid:
        return ""
    try:
        profile = get_profile(uid)
        week = stats(uid, days=7)
        plan = get_plan(uid)
    except Exception:
        return ""
    if not profile and not week["sessions"] and not plan:
        return ""
    payload = {
        "sport_profile": profile,
        "last_7_days": {k: week[k] for k in ("sessions", "minutes", "distance_km", "by_sport", "streak_days")},
        "plan": {k: plan.get(k) for k in ("goal", "weeks", "days_per_week", "session_minutes", "items")} if plan else {},
    }
    return "【运动数据】用户已保存的运动档案、近 7 天训练量和当前计划：\n" + data_block(
        "memory", json.dumps(payload, ensure_ascii=False)
    )
