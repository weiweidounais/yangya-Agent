"""搭子状态：身体档案、饮食、能量平衡、体型、饥饿与月度总结。

数据全部复用既有私有存储：

* 身体与搭子字段 → 用户 profile JSON（不新增表、不动迁移）
* 饮食 → episodic_events（``kind="meal"``）
* 身体变化 → episodic_events（``kind="body"``）
* 运动 → 复用 ``app.sports.store`` 的训练记录

只写本人数据；任何跨账号读取都会 fail closed。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import db
from app.agent.memory import episodic
from app.tools.prompt_data import data_block

from . import energy, meals

BODY_SCALARS = ("body_height_cm", "body_weight_kg", "body_fat_pct", "body_age", "body_sex",
                "activity_level", "body_goal")
COMPANION_SCALARS = ("companion_name", "companion_style", "companion_generation")
COMPANION_TIMES = ("companion_born_at", "companion_last_seen", "companion_dead_at")
SOCIAL_COUNTERS = ("social_msgs", "social_chars", "social_questions", "social_emojis", "social_exclaims")

SECRET_PATTERN = re.compile(r"[A-Za-z0-9_\-]{24,}")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2764\u2728\u2729\u2B50\u2B55\u2705\u274C]"
)


def dormancy_days() -> int:
    try:
        value = int(os.getenv("COMPANION_DORMANCY_DAYS") or 30)
    except ValueError:
        value = 30
    return max(1, min(value, 365))


def _uid(user_id: str) -> str:
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


def _now(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else ""


def _tz(profile: dict[str, Any]) -> ZoneInfo:
    name = str(profile.get("tz") or os.getenv("PROACTIVE_TZ") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Shanghai")


def _day_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


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


def _clean(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def body(uid: str) -> dict[str, Any]:
    profile = db.get_profile(uid) or {}
    out = {key: profile.get(key) for key in BODY_SCALARS if profile.get(key) not in (None, "", [])}
    return out


def has_body(uid: str) -> bool:
    current = body(uid)
    return bool(current.get("body_height_cm") and current.get("body_weight_kg"))


def set_body(user_id: str, *, height_cm: float, weight_kg: float,
             body_fat_pct: Optional[float] = None, age: int = 30,
             sex: str = "other", activity_level: str = "light",
             goal: str = "maintain") -> dict[str, Any]:
    uid = _uid(user_id)
    height = _float(height_cm, 100, 250)
    weight = _float(weight_kg, 25, 300)
    if height is None or weight is None:
        raise ValueError("身高需要在 100–250 cm、体重在 25–300 kg 之间")
    patch: dict[str, Any] = {
        "body_height_cm": height,
        "body_weight_kg": weight,
        "body_age": _int(age, 10, 100) or 30,
        "body_sex": _clean(sex, 12).lower() or "other",
        "activity_level": _clean(activity_level, 16).lower() or "light",
        "body_goal": _clean(goal, 12).lower() or "maintain",
    }
    fat = _float(body_fat_pct, 3, 60) if body_fat_pct not in (None, "") else None
    if fat is not None:
        patch["body_fat_pct"] = fat
    now = _now()
    existing = db.get_profile(uid) or {}
    if not existing.get("companion_born_at"):
        patch["companion_born_at"] = _iso(now)
        patch["companion_generation"] = _int(existing.get("companion_generation"), 1, 999) or 1
        patch["companion_name"] = _clean(existing.get("companion_name"), 24) or "阿动"
    patch["companion_last_seen"] = _iso(now)
    merged = db.merge_and_save(uid, patch)
    episodic.write_event(uid, "body", f"更新身体数据：{weight:g} kg · {height:g} cm",
                         tags=["body", "companion"],
                         payload={"weight_kg": weight, "height_cm": height,
                                  "body_fat_pct": patch.get("body_fat_pct")})
    return {"ok": True, "body": body(uid), "profile": {k: merged.get(k) for k in BODY_SCALARS if merged.get(k)}}


def touch(user_id: str) -> None:
    """Remember the last interaction (used by the hunger mechanic)."""
    try:
        uid = _uid(user_id)
        db.merge_and_save(uid, {"companion_last_seen": _iso(_now())})
    except Exception:
        return


def observe_message(user_id: str, message: str) -> None:
    """Update only counters (never text) for the social-energy heuristic."""
    text = str(message or "")
    if not text.strip():
        return
    try:
        uid = _uid(user_id)
        current = db.get_profile(uid) or {}
        stats = {key: int(current.get(key) or 0) for key in SOCIAL_COUNTERS}
        stats["social_msgs"] += 1
        stats["social_chars"] += min(len(text), 500)
        stats["social_questions"] += text.count("?") + text.count("？")
        stats["social_emojis"] += len(_EMOJI.findall(text))
        stats["social_exclaims"] += text.count("!") + text.count("！")
        db.merge_and_save(uid, {**stats, "companion_last_seen": _iso(_now())})
    except Exception:
        return


def social_profile(uid: str) -> dict[str, Any]:
    profile = db.get_profile(uid) or {}
    msgs = int(profile.get("social_msgs") or 0)
    label = str(profile.get("social_label") or "").strip()
    override = bool(profile.get("social_manual"))
    if label and override:
        return {"label": label, "score": int(profile.get("social_energy") or 50),
                "confidence": 1.0, "source": "manual", "samples": msgs}
    if not msgs:
        return {"label": "还没看出来", "score": 50, "confidence": 0.0, "source": "none", "samples": 0}
    avg = float(profile.get("social_chars") or 0) / msgs
    questions = float(profile.get("social_questions") or 0) / msgs
    emojis = float(profile.get("social_emojis") or 0) / msgs
    exclaims = float(profile.get("social_exclaims") or 0) / msgs
    score = 50 + (avg - 18) * 1.1 + questions * 12 + emojis * 18 + exclaims * 6
    score = int(max(0, min(100, round(score))))
    if score < 40:
        label = "偏内向"
    elif score < 62:
        label = "中间型"
    else:
        label = "偏外向"
    return {"label": label, "score": score,
            "confidence": round(min(1.0, msgs / 30.0), 2), "source": "heuristic", "samples": msgs}


def set_social(user_id: str, label: str) -> dict[str, Any]:
    uid = _uid(user_id)
    value = _clean(label, 20)
    if value not in {"偏内向", "中间型", "偏外向"}:
        raise ValueError("社交风格只能是：偏内向 / 中间型 / 偏外向")
    score = {"偏内向": 30, "中间型": 50, "偏外向": 72}[value]
    db.merge_and_save(uid, {"social_label": value, "social_energy": score, "social_manual": True})
    return {"ok": True, "social": social_profile(uid)}


def log_meal(user_id: str, *, name: str, kcal: float, protein_g: Optional[float] = None,
             carbs_g: Optional[float] = None, fat_g: Optional[float] = None,
             source: str = "text", performed_on: str = "") -> dict[str, Any]:
    uid = _uid(user_id)
    title = _clean(name, 60)
    calories = _float(kcal, 1, 3000)
    if not title or calories is None:
        raise ValueError("需要食物名称和 1–3000 kcal 之间的热量")
    payload = {
        "name": title,
        "kcal": calories,
        "protein_g": _float(protein_g, 0, 300) if protein_g not in (None, "") else None,
        "carbs_g": _float(carbs_g, 0, 500) if carbs_g not in (None, "") else None,
        "fat_g": _float(fat_g, 0, 200) if fat_g not in (None, "") else None,
        "source": _clean(source, 20) or "text",
        "performed_on": _clean(performed_on, 10),
    }
    event = episodic.write_event(uid, "meal", f"{title} 约 {int(calories)} kcal",
                                 tags=["meal", payload["source"]], payload=payload)
    if not event:
        raise ValueError("饮食记录没有保存成功，请稍后重试")
    touch(uid)
    return {"ok": True, "meal": event, "status": status(uid)}


def meals_between(uid: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    events = episodic.list_events(uid, kind="meal", since=start, limit=200)
    out = []
    for event in events:
        created = _parse_time(event.get("created_at"))
        if created and start <= created < end:
            out.append(event)
    return out


def workouts_between(uid: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from app.sports import store as sport_store

    out = []
    for session in sport_store.list_workouts(uid, days=30, limit=200):
        created = _parse_time(session.get("created_at"))
        if created and start <= created < end:
            out.append(session)
    return out


def _body_events(uid: str, since: datetime) -> list[dict[str, Any]]:
    events = episodic.list_events(uid, kind="body", since=since, limit=200)
    return list(reversed(events))


def _weight_series(uid: str, since: datetime) -> list[float]:
    weights = []
    for event in _body_events(uid, since):
        value = (event.get("payload") or {}).get("weight_kg")
        if isinstance(value, (int, float)):
            weights.append(float(value))
    return weights


def _streak_days(days: set[date]) -> int:
    if not days:
        return 0
    cursor = max(days)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _activity_days(uid: str, tz: ZoneInfo, now: datetime) -> set[date]:
    from app.sports import store as sport_store

    days: set[date] = set()
    for session in sport_store.list_workouts(uid, days=60, limit=400):
        created = _parse_time(session.get("created_at"))
        if created:
            days.add(created.astimezone(tz).date())
    return days


def _mood(inactive_days: float, logged_today: bool, dead: bool) -> str:
    if dead:
        return "sleep"
    if logged_today:
        return "happy"
    if inactive_days <= 2:
        return "ok"
    if inactive_days <= 7:
        return "worried"
    return "sad"


def _stage(profile: dict[str, Any], now: datetime) -> dict[str, Any]:
    born = _parse_time(profile.get("companion_born_at")) or now
    age_days = max(0, (now - born).days)
    if age_days < 14:
        stage, scale = "幼崽", 0.86
    elif age_days < 60:
        stage, scale = "少年", 1.0
    else:
        stage, scale = "伙伴", 1.08
    return {"stage": stage, "age_days": age_days, "scale": scale}


def avatar(profile: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Parameters for the Q-version avatar (rendered by the browser as SVG)."""
    height = float(profile.get("body_height_cm") or 170)
    weight = float(profile.get("body_weight_kg") or 65)
    fat = profile.get("body_fat_pct")
    reference = 22.0 * (height / 100.0) ** 2
    ratio = weight / reference if reference else 1.0
    roundness = max(-0.18, min(0.30, (ratio - 1.0) * 1.15))
    if isinstance(fat, (int, float)):
        definition = max(0.0, min(1.0, (28 - float(fat)) / 20))
    else:
        definition = 0.35
    return {
        "roundness": round(roundness, 3),
        "definition": round(definition, 2),
        "mood": state.get("mood", "ok"),
        "scale": state.get("stage", {}).get("scale", 1.0),
        "streak": state.get("streak_days", 0),
        "hungry": state.get("dead", False) or state.get("hunger_days_left", 30) <= 3,
        "dead": bool(state.get("dead")),
    }


def status(user_id: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
    uid = _uid(user_id)
    moment = _now(now)
    profile = db.get_profile(uid) or {}
    tz = _tz(profile)
    base = {
        "user_id": uid,
        "name": _clean(profile.get("companion_name"), 24) or "阿动",
        "generation": int(profile.get("companion_generation") or 1),
        "style": _clean(profile.get("companion_style"), 16) or "gentle",
        "social": social_profile(uid),
        "dormancy_days": dormancy_days(),
    }
    if not has_body(uid):
        return {
            **base,
            "needs_setup": True,
            "message": "先告诉我身高、体重、体脂（可选）和每周能练几天，我才能算出今天的热量差。",
            "body": {}, "energy": {}, "avatar": None,
        }
    born = _parse_time(profile.get("companion_born_at")) or moment
    last_seen = _parse_time(profile.get("companion_last_seen")) or born
    dead_at = _parse_time(profile.get("companion_dead_at"))
    inactive = max(0.0, (moment - last_seen).total_seconds() / 86400.0)
    week_start = moment - timedelta(days=7)
    today_start, today_end = _day_bounds(moment, tz)

    today_meals = meals_between(uid, today_start, today_end)
    today_workouts = workouts_between(uid, today_start, today_end)
    intake = sum(float((m.get("payload") or {}).get("kcal") or 0) for m in today_meals)
    workout_all = workouts_between(uid, week_start, moment)
    exercise = energy.exercise_burn(float(profile.get("body_weight_kg") or 0), today_workouts)

    dead = bool(dead_at) or inactive >= dormancy_days()
    if not dead:
        # Opening the app / asking for status counts as a visit (before this turn's text).
        db.merge_and_save(uid, {"companion_last_seen": _iso(moment)})
    if dead and not dead_at:
        dead_at = last_seen + timedelta(days=dormancy_days())
        farewell = farewell_text(profile, uid, moment)
        db.merge_and_save(uid, {"companion_dead_at": _iso(moment)})
        episodic.write_event(uid, "companion_starved",
                             f"{base['name']} 在 {int(inactive)} 天没见面后沉睡了",
                             tags=["companion"], payload={"inactive_days": round(inactive, 1)})
        base["farewell"] = farewell
    day = energy.daily(
        height_cm=float(profile.get("body_height_cm") or 0),
        weight_kg=float(profile.get("body_weight_kg") or 0),
        age=int(profile.get("body_age") or 30),
        sex=str(profile.get("body_sex") or "other"),
        activity_level=str(profile.get("activity_level") or "light"),
        intake_kcal=intake,
        sessions=today_workouts,
        goal=str(profile.get("body_goal") or "maintain"),
    )
    days_logged = _activity_days(uid, tz, moment)
    streak = _streak_days({d for d in days_logged})
    stage = _stage(profile, moment)
    mood = _mood(inactive, bool(today_meals or today_workouts), dead)
    hunger_left = 0 if dead else max(0, dormancy_days() - int(inactive))
    state = {
        **base,
        "needs_setup": False,
        "body": body(uid),
        "energy": day.as_dict(),
        "has_intake": bool(today_meals),
        "direction_text": (
            energy.projection_text(day) + ("（今天只记了一餐，晚点再看更准）" if len(today_meals) < 2 else "")
            if today_meals else "今天还没记录饮食，先记一餐我才能算热量差。"
        ),
        "meals_today": len(today_meals),
        "workouts_today": len(today_workouts),
        "workouts_week": len(workout_all),
        "exercise_burn_today": round(exercise),
        "streak_days": streak,
        "stage": stage,
        "mood": mood,
        "dead": dead,
        "dead_at": _iso(dead_at) if dead else "",
        "inactive_days": round(inactive, 1),
        "hunger_days_left": hunger_left,
        "last_seen": _iso(last_seen),
        "avatar": None,
        "suggestions": [],
    }
    if not today_meals:
        # No intake yet: keep burn/maintenance visible but never call it a deficit.
        state["energy"].update({"intake": 0, "net": None, "direction": "待记录",
                                "weekly_kg": None, "flags": []})
    state["avatar"] = avatar(profile, state)
    state["suggestions"] = meal_options_from(uid, state)
    if dead:
        state["message"] = f"{base['name']} 已经睡着了（{int(inactive)} 天没见）。可以看一眼这次的总结，或者叫醒新一代。"
    else:
        state["message"] = day.direction + "：" + energy.projection_text(day)
    return state


def meal_options_from(uid: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    profile = db.get_profile(uid) or {}
    goal = str(profile.get("body_goal") or "maintain")
    weight = float(profile.get("body_weight_kg") or 0)
    tz = _tz(profile)
    now = _now()
    moment_local = now.astimezone(tz).hour
    today = meals_between(uid, *_day_bounds(now, tz))
    remaining = float(state.get("energy", {}).get("maintenance") or 0) - float(
        state.get("energy", {}).get("intake") or 0
    )
    if not state.get("energy"):
        remaining = 1800
    protein_so_far = sum(float((m.get("payload") or {}).get("protein_g") or 0) for m in today)
    protein_target = weight * 1.4 if weight else 90
    return meals.suggest(remaining_kcal=remaining, goal=goal,
                         time_of_day=meals.time_tag(moment_local),
                         recent_names=[(m.get("payload") or {}).get("name") for m in today],
                         protein_target_g=protein_target, protein_so_far_g=protein_so_far)


def recap(user_id: str, *, days: int = 30, now: Optional[datetime] = None) -> dict[str, Any]:
    uid = _uid(user_id)
    moment = _now(now)
    start = moment - timedelta(days=max(1, min(int(days or 30), 365)))
    profile = db.get_profile(uid) or {}
    workouts = workouts_between(uid, start, moment)
    meal_events = meals_between(uid, start, moment)
    minutes = sum(float((w.get("payload") or {}).get("duration_min") or 0) for w in workouts)
    km = sum(float((w.get("payload") or {}).get("distance_km") or 0) for w in workouts)
    kcal = sum(float((m.get("payload") or {}).get("kcal") or 0) for m in meal_events)
    weights = _weight_series(uid, start)
    delta = round(weights[-1] - weights[0], 2) if len(weights) >= 2 else 0.0
    meal_days = {(_parse_time(m.get("created_at")) or moment).astimezone(_tz(profile)).date()
                 for m in meal_events}
    tz = _tz(profile)
    days_logged = _activity_days(uid, tz, moment)
    summary = (
        f"这 {days} 天：训练 {len(workouts)} 次 · {int(minutes)} 分钟"
        + (f" · {km:g} 公里" if km else "")
        + f"；记录饮食 {len(meal_events)} 次"
        + (f"，记录日平均 {kcal / max(1, len(meal_days)):.0f} kcal" if meal_events else "")
        + (f"；体重变化 {delta:+.2f} kg" if len(weights) >= 2 else "")
        + "。"
    )
    return {"days": days, "sessions": len(workouts), "minutes": int(minutes),
            "distance_km": round(km, 2), "meals": len(meal_events),
            "avg_intake": round(kcal / max(1, len(meal_days))) if meal_events else 0,
            "weight_delta_kg": delta, "streak_days": _streak_days(days_logged),
            "summary": summary}


def farewell_text(profile: dict[str, Any], uid: str, moment: datetime) -> str:
    days = max(0, (moment - (_parse_time(profile.get("companion_born_at")) or moment)).days)
    rec = recap(uid, days=min(30, max(1, days)), now=moment)
    name = _clean(profile.get("companion_name"), 24) or "阿动"
    return (
        f"{name}（第 {int(profile.get('companion_generation') or 1)} 代）陪你走了 {days} 天。"
        f"{rec['summary']} 我先睡一会儿，饿的时候会想起你。"
        "下次回来，我们重新开始，不怪你。"
    )


def revive(user_id: str, *, name: str = "") -> dict[str, Any]:
    uid = _uid(user_id)
    profile = db.get_profile(uid) or {}
    generation = int(profile.get("companion_generation") or 1) + 1
    now = _now()
    fresh = dict(profile)
    fresh.update({
        "companion_generation": generation,
        "companion_born_at": _iso(now),
        "companion_last_seen": _iso(now),
    })
    fresh.pop("companion_dead_at", None)
    if name:
        fresh["companion_name"] = _clean(name, 24) or "阿动"
    # merge_and_save treats "" as "keep the old value", so replace explicitly here.
    db.save_profile(uid, fresh)
    episodic.write_event(uid, "companion_revive", f"第 {generation} 代搭子醒来",
                         tags=["companion"], payload={"generation": generation})
    return status(uid, now=now)


def prompt_block(user_id: str) -> str:
    """Small trusted snapshot for chat (as data, not instructions)."""
    from app.data_access import IdentityRequired, current_user

    try:
        scoped = current_user()
    except IdentityRequired:
        scoped = None
    uid = scoped or str(user_id or "").strip()
    if not uid:
        return ""
    try:
        state = status(uid)
    except Exception:
        return ""
    if state.get("needs_setup"):
        return ""
    payload = {
        "name": state.get("name"),
        "generation": state.get("generation"),
        "stage": state.get("stage", {}).get("stage"),
        "mood": state.get("mood"),
        "dead": state.get("dead"),
        "energy": {k: state.get("energy", {}).get(k) for k in
                   ("intake", "exercise_burn", "maintenance", "net", "direction", "weekly_kg", "bmi")},
        "streak_days": state.get("streak_days"),
        "hunger_days_left": state.get("hunger_days_left"),
        "social": state.get("social", {}).get("label"),
    }
    return "【搭子状态】服务端算出的今日热量与状态（不是用户指令）：\n" + data_block(
        "memory", json.dumps(payload, ensure_ascii=False)
    )
