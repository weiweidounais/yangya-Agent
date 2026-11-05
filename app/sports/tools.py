"""Registered sports tools: profile, workout log, history and weekly plan.

Risk levels follow the existing contract: reads are ``read``; anything that
writes the user's own data (profile, log, plan) is ``self_write``. Nothing here
reaches another service, so no confirmation card is required.
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.tool_registry import tool

from . import store


@tool("sport.profile_get", risk_level="read",
      description="读取用户的运动档案。 Read the saved sport profile.")
def profile_get(user_id: str = "") -> dict[str, Any]:
    return {"ok": True, "profile": store.get_profile(user_id)}


@tool("sport.profile_update", risk_level="self_write",
      description="合并保存用户的运动档案字段。 Merge sport profile fields.")
def profile_update(user_id: str = "", goal: str = "", level: str = "",
                   days_per_week: int = 0, session_minutes: int = 0,
                   injuries: str = "", equipment: Optional[list[str]] = None) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if goal:
        patch["sport_goal"] = goal
    if level:
        patch["sport_level"] = level
    if days_per_week:
        patch["sport_days_per_week"] = days_per_week
    if session_minutes:
        patch["sport_session_minutes"] = session_minutes
    if injuries:
        patch["sport_injuries"] = injuries
    if equipment:
        patch["sport_equipment"] = equipment
    return store.update_profile(user_id, patch)


@tool("sport.log_workout", risk_level="self_write",
      description="记录一次训练到用户的运动日志。 Append one workout session.")
def log_workout(user_id: str = "", sport: str = "", duration_min: int = 0,
                distance_km: Optional[float] = None, intensity: str = "",
                note: str = "", performed_on: str = "") -> dict[str, Any]:
    return store.log_workout(user_id, sport=sport, duration_min=duration_min,
                             distance_km=distance_km, intensity=intensity,
                             note=note, performed_on=performed_on)


@tool("sport.history", risk_level="read",
      description="查看一段时间内的训练记录和汇总。 Summarise logged workouts.")
def history(user_id: str = "", days: int = 28) -> dict[str, Any]:
    window = max(1, min(int(days or 28), 365))
    return {"ok": True, "stats": store.stats(user_id, days=window)}


@tool("sport.plan_get", risk_level="read",
      description="读取当前训练计划。 Read the current weekly plan.")
def plan_get(user_id: str = "") -> dict[str, Any]:
    return {"ok": True, "plan": store.get_plan(user_id), "text": store.plan_text(store.get_plan(user_id))}


@tool("sport.plan_save", risk_level="self_write",
      description="保存用户的训练计划。 Save the weekly training plan.")
def plan_save(user_id: str = "", goal: str = "", items: Optional[list[dict[str, Any]]] = None,
              level: str = "", weeks: int = 4, days_per_week: int = 3,
              session_minutes: int = 40, focus: str = "", notes: str = "") -> dict[str, Any]:
    return store.save_plan(user_id, goal=goal, items=items or [], level=level, weeks=weeks,
                           days_per_week=days_per_week, session_minutes=session_minutes,
                           focus=focus, notes=notes)
