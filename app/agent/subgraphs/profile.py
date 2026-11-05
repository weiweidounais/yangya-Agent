"""Profile subgraph — merge user-supplied profile fields into the saved profile."""

from __future__ import annotations

from typing import Any

from app import db
from app.agent.memory import write_event
from app.state import AgentState

PROFILE_FIELDS = (
    "name", "city", "tz", "occupation", "interests", "preferences", "facts",
    "sport_goal", "sport_level", "sport_days_per_week", "sport_session_minutes",
    "sport_injuries", "sport_equipment",
)


def _uid(state: AgentState) -> str:
    return state.get("user_id") or "demo"


def _tid(state: AgentState) -> str:
    return str(state.get("thread_id") or "")


def load_profile(state: AgentState) -> dict[str, Any]:
    uid = _uid(state)
    profile = dict(state.get("profile") or {})
    stored = db.get_profile(uid)
    # Missing fields in a saved profile are deletions, not checkpoint fallbacks.
    # forget_all retains memory_rev, so even an otherwise empty profile wins.
    return stored if stored else profile


def node_update_profile(state: AgentState) -> dict[str, Any]:
    uid = _uid(state)
    incoming = dict(state.get("profile") or {})
    patch: dict[str, Any] = {}
    for key in PROFILE_FIELDS:
        value = incoming.get(key)
        if value in (None, "", []):
            continue
        patch[key] = value
    # Privacy settings are not parsed facts; they must survive for later leak guards.
    if incoming.get("privacy_sandbox"):
        patch["privacy_sandbox"] = incoming["privacy_sandbox"]
    if not patch:
        existing = db.get_profile(uid)
        return {
            "profile": existing,
            "messages": [{"role": "assistant", "content": "没有需要更新的档案内容。"}],
        }
    profile = db.merge_and_save(uid, patch)
    changed = [k for k in PROFILE_FIELDS if k in patch]
    msg = "已更新个人档案：" + "、".join(_LABELS.get(k, k) for k in changed) + "。"
    write_event(
        uid,
        "profile_updated",
        msg,
        tags=["profile", "memory"],
        payload={"fields": changed},
        thread_id=_tid(state),
    )
    return {
        "profile": profile,
        "messages": [{"role": "assistant", "content": msg}],
    }


_LABELS = {
    "name": "姓名",
    "city": "城市",
    "tz": "时区",
    "occupation": "职业",
    "interests": "兴趣",
    "preferences": "偏好",
    "facts": "备注",
    "sport_goal": "运动目标",
    "sport_level": "训练水平",
    "sport_days_per_week": "每周训练天数",
    "sport_session_minutes": "单次时长",
    "sport_injuries": "伤病与限制",
    "sport_equipment": "器械与场地",
}
