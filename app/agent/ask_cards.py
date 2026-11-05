"""Structured question cards (single / multi / text / secret) in chat replies.

The model appends one ``<ask>{json}</ask>`` block when it needs the user to choose.
``split_reply`` validates it; anything malformed degrades to plain text and never raises.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from app.prompts import load_prompt
from app import secrets_vault

ASK_TYPES = ("single", "multi", "text", "secret")
_TYPE_ALIASES = {
    "radio": "single",
    "choice": "single",
    "select": "single",
    "checkbox": "multi",
    "multiple": "multi",
    "input": "text",
    "free_text": "text",
    "password": "secret",
    "credential": "secret",
}
MAX_QUESTION = 120
MAX_OPTION = 40
MIN_OPTIONS = 2
MAX_OPTIONS = 8
DEFAULT_TEXT_LABEL = "补充说明（选填）"
_OTHER_OPTION = re.compile(r"^(?:或者|还是)?(?:其他|其它|别的|都不是|以上都不是|不确定)[^\n]{0,8}$")
_BLOCK = re.compile(r"<ask>(.*?)(?:</ask>|$)", re.S | re.I)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)

ASK_GUIDE = load_prompt("chat.ask_guide", "v2")

# Background memory does not emit a chat card on its own. An ask-first consumer
# can register a handler to queue an existing question card for the next turn.
# The handler receives a committed, owner-scoped fact (id/text/confirmed=False);
# it must not treat notification as user confirmation. No handler means no-op.
_pending_fact_handler = None


def set_pending_fact_handler(handler) -> None:
    """Install the pending-memory adapter, or pass None to restore the no-op."""
    global _pending_fact_handler
    _pending_fact_handler = handler


def on_pending_memory_fact(fact: dict[str, Any]) -> None:
    handler = _pending_fact_handler
    if handler is not None:
        handler(fact)


def saved_secrets_note(labels: list[str]) -> str:
    if not labels:
        return ""
    return (
        "【已安全保存】用户已经通过安全输入区保存了：" + "、".join(labels[:8])
        + "。你看不到具体内容，不要再索要，也不要猜测。"
    )


def _new_id() -> str:
    return "ask_" + uuid.uuid4().hex[:10]


def _short(value: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:limit]


def _option_label(opt: Any) -> str:
    if isinstance(opt, dict):
        opt = opt.get("label") or opt.get("text") or opt.get("value") or ""
    if not isinstance(opt, (str, int, float)):
        return ""
    return _short(str(opt).lstrip("-*•· ").strip(), MAX_OPTION)


def validate_ask(obj: Any) -> Optional[dict[str, Any]]:
    """Return a normalized card or None when the payload is not usable."""
    if not isinstance(obj, dict):
        return None
    t = str(obj.get("type") or "").strip().lower()
    t = _TYPE_ALIASES.get(t, t)
    if t not in ASK_TYPES:
        return None

    if t == "secret":
        key = secrets_vault.resolve_key(str(obj.get("platform") or obj.get("key") or ""), str(obj.get("kind") or ""))
        if not key:
            return None
        return secret_card(key, purpose=_short(obj.get("purpose"), 80))

    question = _short(obj.get("question") or obj.get("title"), MAX_QUESTION)
    if not question:
        return None
    card: dict[str, Any] = {"id": _new_id(), "type": t, "question": question}

    if t == "text":
        card["placeholder"] = _short(obj.get("placeholder"), 60)
        card["text_label"] = _short(obj.get("text_label") or obj.get("label"), 40) or "你的回答"
        return card

    raw = obj.get("options")
    if not isinstance(raw, list):
        return None
    options: list[str] = []
    saw_other = False
    for opt in raw:
        label = _option_label(opt)
        if not label:
            continue
        if _OTHER_OPTION.match(label):
            saw_other = True
            continue
        if label not in options:
            options.append(label)
    options = options[:MAX_OPTIONS]
    if len(options) < MIN_OPTIONS:
        return None
    card["options"] = options
    allow = obj.get("allow_text")
    card["allow_text"] = True if allow is None or saw_other else bool(allow)
    if card["allow_text"]:
        card["text_label"] = _short(obj.get("text_label"), 40) or DEFAULT_TEXT_LABEL
        card["placeholder"] = _short(obj.get("placeholder"), 60)
    if t == "multi":
        try:
            mx = int(obj.get("max_select") or 0)
        except (TypeError, ValueError):
            mx = 0
        if 1 < mx < len(options):
            card["max_select"] = mx
    return card


def secret_card(key: str, *, purpose: str = "") -> dict[str, Any]:
    spec = secrets_vault.spec_for(key)
    return {
        "id": _new_id(),
        "type": "secret",
        "key": key,
        "platform": spec["platform"],
        "label": spec["label"],
        "purpose": purpose or "仅在你授权的操作里使用",
        "fields": secrets_vault.public_fields(key),
    }


def as_plain_text(card: Optional[dict[str, Any]]) -> str:
    """Plain-text rendering for channels without cards (Feishu / WeChat)."""
    if not card:
        return ""
    if card.get("type") == "secret":
        return f"需要「{card.get('label')}」时，请打开网页版，在安全输入区填写；别在这里直接发送。"
    lines = [str(card.get("question") or "")]
    for opt in card.get("options") or []:
        lines.append(f"- {opt}")
    return "\n".join(x for x in lines if x)


def _loads(raw: str) -> Any:
    s = _FENCE.sub("", (raw or "").strip())
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(s[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _salvage(obj: Any, raw: str) -> str:
    """Best-effort plain text for an ask block we could not validate."""
    if isinstance(obj, dict):
        q = _short(obj.get("question"), MAX_QUESTION)
        opts = [_option_label(o) for o in obj.get("options") or []] if isinstance(obj.get("options"), list) else []
        lines = [q] + [f"- {o}" for o in opts if o]
        return "\n".join(x for x in lines if x)
    m = re.search(r'"question"\s*:\s*"([^"]{1,%d})"' % MAX_QUESTION, raw or "")
    return m.group(1) if m else ""


def _drop_option_echo(text: str, options: list[str]) -> str:
    """Remove bullet lines in the prose that just repeat the card's options."""
    if not options:
        return text
    norm = {re.sub(r"\s+", "", o).lower() for o in options}
    kept = []
    for line in text.split("\n"):
        stripped = re.sub(r"^\s*(?:[-*•·]|\d+[.)、])\s*", "", line)
        if stripped != line and re.sub(r"\s+", "", stripped).lower() in norm:
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def split_reply(raw: str) -> tuple[str, Optional[dict[str, Any]]]:
    """Split model output into (prose, card|None). Never raises."""
    try:
        return _split(str(raw or ""))
    except Exception:
        return re.sub(r"<ask>.*?(?:</ask>|$)", "", str(raw or ""), flags=re.S | re.I).strip(), None


def _split(raw: str) -> tuple[str, Optional[dict[str, Any]]]:
    stripped = raw.strip()
    # Whole-reply JSON: {"reply": "...", "ask": {...}}
    if stripped.startswith("{") or stripped.startswith("```"):
        whole = _loads(stripped)
        if isinstance(whole, dict) and ("reply" in whole or "ask" in whole):
            text = str(whole.get("reply") or "").strip()
            card = validate_ask(whole.get("ask")) if whole.get("ask") else None
            if whole.get("ask") and not card:
                text = "\n\n".join(x for x in (text, _salvage(whole.get("ask"), "")) if x)
            if card:
                text = _drop_option_echo(text, card.get("options") or [])
            return text, card

    blocks = list(_BLOCK.finditer(raw))
    if not blocks:
        return stripped, None
    text = _BLOCK.sub("", raw).strip()
    inner = blocks[-1].group(1)
    obj = _loads(inner)
    card = validate_ask(obj)
    if card is None:
        salvage = _salvage(obj, inner)
        if salvage and salvage not in text:
            text = "\n\n".join(x for x in (text, salvage) if x)
        return text, None
    return _drop_option_echo(text, card.get("options") or []), card


def answer_context(reply_to: Optional[dict[str, Any]]) -> str:
    """Prefix for the LLM so a card answer keeps the question it answers."""
    if not isinstance(reply_to, dict):
        return ""
    q = _short(reply_to.get("question"), MAX_QUESTION)
    if not q:
        return ""
    lead = _short(reply_to.get("prompt"), 300)
    parts = []
    if lead:
        parts.append(f"你上一条说：「{lead}」")
    parts.append(f"并问用户：「{q}」。下面是用户在选项卡里的回答：")
    return "\n".join(parts)


INTENT_QUESTION = "你想先做哪件事？"
INTENT_LABELS = {
    "draft": "帮我写一段文字",
    "update_profile": "更新我的档案",
    "confirm": "确认当前任务",
    "chat": "只是聊聊",
}


_GENERIC_TASKS = ("draft", "update_profile")


def intent_card(candidates: list[str]) -> dict[str, Any]:
    tasks = list(dict.fromkeys(i for i in candidates if i != "chat" and i in INTENT_LABELS))[:3]
    # An unresolved message has no task candidate; a one-option card would be
    # dropped by validation, so offer the generic tasks instead of a dead end.
    if not tasks:
        tasks = list(_GENERIC_TASKS)
    return validate_ask({
        "type": "single", "question": INTENT_QUESTION,
        "options": [INTENT_LABELS[i] for i in tasks + ["chat"]], "allow_text": True,
    })


def selected_intent(message: str, reply_to: Any) -> Optional[str]:
    if (reply_to or {}).get("question") != INTENT_QUESTION:
        return None
    return next((i for i, label in INTENT_LABELS.items() if message.strip() == label), None)
