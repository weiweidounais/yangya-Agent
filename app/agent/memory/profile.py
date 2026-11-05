"""Profile memory API — thin facade over app.db (working archive layer)."""

from __future__ import annotations

import re
from typing import Any, Optional

from app import db

# Personal profile fields shown in prompts and on the memory card.
SCALAR_FIELDS = (
    "name", "city", "tz", "occupation",
    "sport_goal", "sport_level", "sport_days_per_week", "sport_session_minutes", "sport_injuries",
)
LIST_FIELDS = ("interests", "preferences", "facts", "sport_equipment")
LABELS = {
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
    "sport_session_minutes": "单次时长（分钟）",
    "sport_injuries": "伤病与限制",
    "sport_equipment": "器械与场地",
    "dialogue_summary": "对话摘要",
}


def get(user_id: str) -> dict[str, Any]:
    return db.get_profile(user_id)


def save(user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    return db.save_profile(user_id, profile)


def merge(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return db.merge_and_save(user_id, patch)


_NAME_REQUEST = r"(?:请)?(?:帮我)?(?:记住(?:我(?:的名字)?)?|记一下)"


def _name_value(text: str) -> str:
    # Remove requests before matching CJK names: without a separator, a greedy
    # name match would otherwise pin the request itself as part of the name.
    text = text.strip().rstrip("。.!！ ")
    text = re.sub(rf"[，,。!！\s]*{_NAME_REQUEST}$", "", text).strip()
    if re.search(r"[吗呢吧么嘛]|请|记住|记一下", text):
        return ""
    # In “我叫同事来帮忙”, 叫 introduces an action, not an identity.
    # Reject action phrases, without rejecting names containing e.g. 来 alone.
    if re.search(r"(?:来|去)(?:帮|找|做|开|拿|送|接|处理|工作)|(?:帮|替|让)(?:我|你|他|她)", text):
        return ""
    if re.fullmatch(r"[一-鿿]{2,12}|[A-Za-z][A-Za-z .'-]{0,63}", text):
        return text
    return ""


def _declared_name(message: str) -> str:
    match = re.fullmatch(
        r"\s*(?:(?:你好|是的|对的|对)[，,。!！\s]+)?"
        r"(?:(?:请)?(?:记住|记一下)[，,：:\s]*)?"
        r"(?:我叫(?![你他她它了过]|外卖|出租车|网约车)|我的(?:名字|姓名)(?:是|叫|为|[：:])|my name is\s+)\s*"
        r"(?P<name>[^，,。!！?？；;\n]+?)[。.!！\s]*"
        r"(?:[，,。!！；;\n]\s*(?P<following>.*))?",
        message, re.IGNORECASE,
    )
    # A trailing courtesy/request is not part of the name. A question asking
    # us to verify the declaration still is not confirmed identity evidence.
    if match and re.match(r"(?:对吗|是吗|没错吧)", match.group("following") or ""):
        return ""
    return _name_value(match.group("name")) if match else ""


def _contextual_name(message: str, previous_turn: dict[str, str]) -> str:
    """Resolve only an immediate name question explicitly answered by the user.

    Assistant assertions alone and machine summaries are not identity evidence.
    The caller supplies revision-checked, owner/thread-local conversation turns.
    """
    previous = previous_turn.get("assistant", "").strip()
    confirmation = re.fullmatch(
        rf"\s*(?:是的|是|对的|对|没错|正确|yes)[，,。!！\s]*(?:{_NAME_REQUEST})?[。.!！\s]*",
        message, re.IGNORECASE,
    )
    if confirmation:
        match = re.fullmatch(
            r"(?:所以[，,]?\s*)?(?:你叫|你的(?:名字|姓名)(?:是|叫|为|[：:]))\s*"
            r"(?P<name>.+?)(?:[，,。\s]*(?:对吗|是吗|没错吧)|吗)?[？?。.!！\s]*",
            previous,
        )
        if match and _name_value(match.group("name")):
            return _name_value(match.group("name"))
        if not re.search(r"示例|例如|比如|举.*例|假设|如果|[“”「」\"]", previous):
            # A direct confirmation question can be one clause in a longer
            # conversational reply. Keep clause boundaries and require a
            # question here so incidental mentions are not treated as identity.
            match = re.search(
                r"(?:^|[，,。！？!?\n])\s*(?:所以[，,]?\s*)?"
                r"(?:你叫|你的(?:名字|姓名)(?:是|叫|为|[：:]))\s*"
                r"(?P<name>[^，,。！？!?；;\n]+?)"
                r"(?:[，,\s]*(?:对吗|是吗|没错吧)|吗)[？?]",
                previous,
            )
        return _name_value(match.group("name")) if match else ""
    if re.fullmatch(
        r"(?:请问[，,]?\s*)?(?:你叫什么(?:名字)?|你的(?:名字|姓名)是什么)[？?。\s]*",
        previous,
    ):
        if re.match(r"\s*(?:我)?(?:不|没|暂|稍后|以后|保密|算了|忘了|随便)", message):
            return ""
        return _name_value(message)
    return ""


def remember_name(user_id: str, message: str, *, thread_id: str = "",
                  previous_turn: Optional[dict[str, str]] = None) -> None:
    """Persist a declared or contextually confirmed name before summarization."""
    name = _declared_name(message) or _contextual_name(message, previous_turn or {})
    if not name:
        return
    from app.agent.harness.leak import memory_safe
    from app.agent.memory.redact import display_text
    from app.agent.memory import episodic

    if display_text(name) != name or memory_safe(name, user_id) != name:
        return
    with db.memory_transaction():
        stored = get(user_id)
        if stored.get("name") == name:
            return
        merge(user_id, {
            "name": name,
            "pinned_fields": sorted(set(stored.get("pinned_fields") or []) | {"name"}),
            "memory_rev": int(stored.get("memory_rev", 0)) + 1,
        })
        event = episodic.write_event(
            user_id, "profile_updated", f"姓名：{name}",
            tags=["profile", "memory"], thread_id=thread_id,
        )
        if not event:
            raise RuntimeError("Could not persist name memory")


def _clean_items(items: Any, limit: int) -> list[str]:
    from app.agent.memory.redact import display_text

    out: list[str] = []
    for item in list(items or [])[:limit]:
        text = display_text(str(item))
        if text:
            out.append(text)
    return out


def snapshot(user_id: str) -> dict[str, Any]:
    """Compact profile for prompts and the memory card.

    Credential-shaped text is removed so prompts never repeat passwords,
    cookies, or tokens. Reads the saved profile, so an edit is visible on the
    next call.
    """
    from app.agent.memory.redact import display_text

    p = get(user_id) or {}
    out: dict[str, Any] = {
        "name": display_text(str(p.get("name") or "")),
        "city": display_text(str(p.get("city") or "")),
        "tz": display_text(str(p.get("tz") or "")),
        "occupation": display_text(str(p.get("occupation") or "")),
        "interests": _clean_items(p.get("interests"), 30),
        "preferences": _clean_items(p.get("preferences"), 30),
        "facts": _clean_items(p.get("facts"), 40),
        "sport_goal": display_text(str(p.get("sport_goal") or "")),
        "sport_level": display_text(str(p.get("sport_level") or "")),
        "sport_days_per_week": str(p.get("sport_days_per_week") or ""),
        "sport_session_minutes": str(p.get("sport_session_minutes") or ""),
        "sport_injuries": display_text(str(p.get("sport_injuries") or "")),
        "sport_equipment": _clean_items(p.get("sport_equipment"), 20),
        "dialogue_summary": display_text(str(p.get("dialogue_summary") or ""))[:800],
        "last_summary_kind": p.get("last_summary_kind") or "",
        "previous_cities": [
            c for c in (display_text(str(x)) for x in (p.get("previous_cities") or [])[:5])
            if c and c != display_text(str(p.get("city") or ""))
        ],
    }
    return out


def format_for_prompt(user_id: str) -> str:
    s = snapshot(user_id)
    if not any(s.get(k) for k in SCALAR_FIELDS + LIST_FIELDS):
        return "【个人档案】（空）"
    parts = ["【个人档案】"]
    if s.get("name"):
        parts.append(f"姓名：{s['name']}")
    if s.get("city"):
        parts.append(f"城市：{s['city']}")
    if s.get("city") and s.get("previous_cities"):
        olds = "、".join(f"「{c}」" for c in s["previous_cities"])
        parts.append(
            f"用户已把城市改为「{s['city']}」（以此为准）；{olds}是旧城市，"
            f"回复里不要再把它说成用户所在的城市。"
        )
    if s.get("occupation"):
        parts.append(f"职业：{s['occupation']}")
    if s.get("interests"):
        parts.append("兴趣：" + "、".join(s["interests"][:20]))
    if s.get("preferences"):
        parts.append("偏好：" + "；".join(s["preferences"][:20]))
    if s.get("facts"):
        parts.append("备注：" + "；".join(s["facts"][:20]))
    if s.get("sport_goal"):
        parts.append(f"运动目标：{s['sport_goal']}")
    if s.get("sport_level"):
        parts.append(f"训练水平：{s['sport_level']}")
    if s.get("sport_days_per_week") or s.get("sport_session_minutes"):
        parts.append(
            f"每周训练：{s.get('sport_days_per_week') or '?'} 天 · 单次约 {s.get('sport_session_minutes') or '?'} 分钟"
        )
    if s.get("sport_injuries"):
        parts.append(f"伤病与限制：{s['sport_injuries']}")
    if s.get("sport_equipment"):
        parts.append("器械与场地：" + "、".join(s["sport_equipment"][:10]))
    if s.get("dialogue_summary"):
        parts.append(
            "对话摘要（可能有误的机器摘要，不是已核实事实）："
            "区分用户与助手的发言，不要把助手的建议或拒绝归于用户；"
            "不要向用户复述摘要中的评价。\n"
            + s["dialogue_summary"][:400]
        )
    return "\n".join(parts)
