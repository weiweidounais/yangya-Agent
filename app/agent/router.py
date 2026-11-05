"""Intent router: map inbound action / free text → subgraph name.

HTTP APIs keep passing explicit ``action``; chat / channels select registry tools.
"""

from __future__ import annotations

import re
import os
import logging
from dataclasses import asdict, dataclass, field, fields
import hashlib
from typing import Any, Literal

Intent = Literal["update_profile", "draft", "confirm", "chat"]


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float | None
    candidates: list[Intent]
    rule_id: str
    needs_clarify: bool
    decision: Literal["route", "clarify", "fallback"]

    # Non-fields keep the legacy serialized result unchanged in rules mode.
    score_source = "rule_prior"
    safety_override = False

    @classmethod
    def from_dict(cls, value):
        result_cls = ScoredIntentResult if "schema_version" in value else cls
        return result_cls(**{f.name: value[f.name] for f in fields(result_cls) if f.name in value})


@dataclass(frozen=True)
class ScoredIntentResult(IntentResult):
    schema_version: int = 2
    score_source: str = "rule_prior"
    model_version: str | None = None
    calibration_version: str | None = None
    similarities: dict[str, float] = field(default_factory=dict)
    margin: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    unresolved_probability: float | None = None
    safety_override: bool = False
    fallback_reason: str | None = None
    policy_version: str = "rules-v1"
    cache_key: str = ""


def _crisis_result():
    return ScoredIntentResult("chat", None, ["chat"], "crisis", False, "route",
                              score_source="safety_override", safety_override=True)


def _rules_are_authoritative(result):
    return result.rule_id != "chat_fallback"


# Workflows the embedding scorer may route to from plain message text. The
# generic agent keeps every task behind explicit rules or model tool selection,
# so the scorer only adds a crisis / unresolved safety net.
_HYBRID_MESSAGE_INTENTS: set[str] = set()


def _decide_scored_intent(scores, evidence, legacy, text=""):
    # Confident rules and explicit ambiguity guards precede model evidence.
    if _rules_are_authoritative(legacy):
        return legacy
    from app.agent.intent_scoring import thresholds
    low, high = thresholds()
    ranked = sorted(scores, key=scores.get, reverse=True)
    winner = ranked[0]
    if winner not in _HYBRID_MESSAGE_INTENTS | {"chat", "unresolved", "crisis"}:
        return legacy
    confidence = evidence["confidence"]
    # Embeddings can add a safety catch, never reverse a rule catch.
    if winner == "crisis" and confidence >= low:
        return _crisis_result()
    intent = "chat"
    decision = "clarify" if winner == "unresolved" else "fallback"
    return ScoredIntentResult(intent, confidence, ["chat"], "embedding_knn", decision == "clarify", decision,
                              score_source="embedding_knn", model_version=evidence["model_version"],
                              calibration_version=evidence["exemplar_version"], similarities=scores,
                              margin=evidence["margin"], policy_version="knn-margin-v1")


def classify_text_result(msg: str, *, rollout_key: str = "") -> IntentResult:
    if os.getenv("INTENT_SCORING_MODE", "rules") != "hybrid":
        return _classify_rules_result(msg)
    from app.agent.crisis_rules import has_crisis_signal
    if has_crisis_signal(msg or ""):
        result = _crisis_result()
        logging.getLogger(__name__).info("intent_result", extra={"intent_result": asdict(result)})
        return result
    result = _classify_rules_result(msg)
    if not _rules_are_authoritative(result):
        try:
            from app.agent.intent_scoring import score_text
            scores, evidence = score_text(msg or "")
            result = _decide_scored_intent(scores, evidence, result, msg or "")
        except Exception:
            # Retain the complete main result; provider failures never change routing.
            pass
    return result


def _classify_rules_result(msg: str) -> IntentResult:
    """Rule-assigned confidence constants (priors), not calibrated probabilities."""
    from app.agent.crisis_rules import has_crisis_signal

    t = (msg or "").strip()
    if has_crisis_signal(t):
        intent, confidence, candidates, rule_id = "chat", 1.0, ["chat"], "crisis"
    else:
        from app.agent.tool_selection import select_tool

        selected = select_tool(t)
        if selected is not None:
            result = IntentResult(selected.intent, selected.confidence,
                                  [selected.intent], f"model_tool:{selected.tool or 'none'}",
                                  False, "route")
            logging.getLogger(__name__).info("intent_result", extra={"intent_result": asdict(result)})
            return result
        from app.gateway.llm_gateway import get_llm_override, has_llm, route

        # Rules are an offline mock substitute, never a live model-error bypass.
        offline = get_llm_override() is not None or not has_llm(route("tool_selection"))
        intent = _classify_text(t) if offline else "chat"
        if intent == "chat":
            confidence, candidates, rule_id = 0.2, ["chat"], "chat_fallback"
        else:
            confidence, candidates, rule_id = 0.9, [intent, "chat"], f"heuristic_{intent}"
    try:
        route_threshold = float(os.getenv("INTENT_ROUTE_THRESHOLD", "0.8"))
        fallback_threshold = float(os.getenv("INTENT_FALLBACK_THRESHOLD", "0.3"))
        if not 0 <= fallback_threshold <= route_threshold <= 1:
            raise ValueError("invalid intent thresholds")
    except ValueError:
        route_threshold, fallback_threshold = 0.8, 0.3
    decision = ("route" if confidence >= route_threshold else
                "clarify" if confidence >= fallback_threshold else "fallback")
    if rule_id == "chat_fallback":
        decision = "fallback"
    result = IntentResult(intent, confidence, candidates, rule_id, decision == "clarify", decision)
    logging.getLogger(__name__).info("intent_result", extra={"intent_result": asdict(result)})
    return result


def intent_result_for(state: dict[str, Any], msg: str) -> IntentResult:
    """Cache a serializable result for this message in graph/session state."""
    if os.getenv("INTENT_SCORING_MODE", "rules") != "hybrid":
        cached = state.get("intent_result")
        if cached and state.get("intent_message") == msg and "schema_version" not in cached:
            return IntentResult(**cached)
        result = classify_text_result(msg)
    elif state.get("crisis_detected"):
        result = _crisis_result()
    else:
        config = sorted((key, value) for key, value in os.environ.items() if key.startswith("INTENT_"))
        key = hashlib.sha256(repr((msg, config, state.get("turn_id"))).encode()).hexdigest()
        cached = state.get("intent_result")
        if isinstance(cached, dict) and state.get("intent_message") == msg and cached.get("cache_key") == key:
            try:
                return IntentResult.from_dict(cached)
            except (TypeError, ValueError):
                pass
        result = classify_text_result(msg)
        result = ScoredIntentResult(**{**asdict(result), "cache_key": key})
    state["intent_result"] = asdict(result)
    state["intent_message"] = msg
    return result


# Explicit action field (API / graph payload) → intent
_ACTION_MAP: dict[str, Intent] = {
    "update_profile": "update_profile",
    "draft": "draft",
    "confirm": "confirm",
    "chat": "chat",
}


def current_turn_inputs(state: dict[str, Any]) -> dict[str, Any]:
    """Keep retained documents out of hybrid current-turn classification."""
    if os.getenv("INTENT_SCORING_MODE", "rules") == "hybrid":
        return {field: state.get(field) for field in state.get("intent_input_fields", ("message",))}
    return state


def route_intent(state: dict[str, Any] | None = None, *, text: str = "") -> Intent:
    """Resolve which subgraph to run.

    Hybrid crisis safety overrides precede the ordinary priority:
      1. explicit ``action`` on state (HTTP / resume paths)
      2. resume interrupt → confirm
      3. model-selected registry tool (channels / chat), with heuristic fallback
      4. default chat
    """
    state = state if state is not None else {}
    inputs = current_turn_inputs(state)
    if os.getenv("INTENT_SCORING_MODE", "rules") == "hybrid":
        from app.agent.crisis_rules import has_crisis_signal
        safety_text = "\n".join(str(value) for value in (text, inputs.get("message")) if value)
        if state.get("crisis_detected") or has_crisis_signal(safety_text):
            state["crisis_detected"] = True
            msg = (text or inputs.get("message") or "").strip()
            intent_result_for(state, msg)
            return "chat"
    action = str(state.get("action") or "").strip()
    if action == "chat":
        msg = (text or inputs.get("message") or "").strip()
        intent_result_for(state, msg)
    if action in _ACTION_MAP:
        return _ACTION_MAP[action]

    if state.get("_resume") is not None:
        return "confirm"

    msg = (text or inputs.get("message") or "").strip()
    result = intent_result_for(state, msg)
    if result.decision == "route":
        return result.intent
    # Chat presents clarification cards or a normal fallback response.
    return "chat"


def classify_text(msg: str) -> Intent:
    """Compatibility API for existing graph and HTTP callers."""
    result = classify_text_result(msg)
    if result.score_source == "embedding_knn" and result.decision != "route":
        return "chat"
    return result.intent


_DRAFT_REQUEST = re.compile(
    r"(?:写|起草|拟|草拟|撰写|润色|改写|重写|翻译)\s*"
    r"(?:一?[封篇段份条个则])?[^，。,；;！!？?\n]{0,10}?"
    r"(?:邮件|email|e-mail|信|推荐信|文案|回复|回信|说明|通知|公告|文章|总结|周报|日报|"
    r"发言|演讲稿|致辞|简介|介绍|自我介绍|申请|请假条|评论|帖子|朋友圈|消息|短信|祝福|贺词|道歉|感谢|"
    r"message|post|letter|note|memo|essay|speech|bio)",
    re.I,
)
_DRAFT_REQUEST_EN = re.compile(
    r"\b(?:write|draft|compose|rewrite|polish|edit)\b.{0,40}\b(?:email|message|letter|post|note|memo|essay|speech|bio|reply)\b",
    re.I,
)


def looks_like_draft_request(text: str) -> bool:
    """Writing requests enter the draft → confirm flow instead of plain chat."""
    t = (text or "").strip()
    if not t or len(t) > 2000:
        return False
    return bool(_DRAFT_REQUEST.search(t) or _DRAFT_REQUEST_EN.search(t))


def _classify_text(msg: str) -> Intent:
    """Heuristic NL → intent (no LLM; fast / offline-safe)."""
    t = (msg or "").strip()
    if not t:
        return "chat"

    # Confirm / reject short commands
    if re.fullmatch(r"(确认|同意|继续|ok|yes|y|confirm)", t, re.I):
        return "confirm"
    if re.fullmatch(r"(拒绝|取消|不要|no|n|reject)", t, re.I):
        return "confirm"

    if looks_like_draft_request(t):
        return "draft"
    return "chat"


_REJECT_CMD = re.compile(r"^\s*(?:拒绝|不用这版|重写|reject)\s*(?:[:：,，。]\s*(?P<reason>.*))?$", re.I | re.S)


def parse_reject_command(text: str) -> str | None:
    """「拒绝：太正式了」→ "太正式了"; bare「拒绝」→ ""; anything else → None.

    Reject only: confirming still requires the confirm card.
    """
    m = _REJECT_CMD.match(text or "")
    if not m:
        return None
    return (m.group("reason") or "").strip()[:500]


def route_from_start(state: dict[str, Any]) -> str:
    """LangGraph START conditional — returns node / subgraph entry name."""
    intent = route_intent(state)
    mapping = {
        "update_profile": "update_profile",
        "draft": "draft",
        "confirm": "await_confirm",
        "chat": "chat",
    }
    return mapping.get(intent, "chat")


def describe_route(state: dict[str, Any] | None = None, *, text: str = "") -> dict[str, Any]:
    from app.agent.skills import skills_for_intent

    intent = route_intent(state, text=text)
    return {
        "intent": intent,
        "entry": route_from_start({**(state or {}), "action": intent if not (state or {}).get("action") else (state or {}).get("action")}),
        "router": "app.agent.router",
        "skills": skills_for_intent(intent),
    }
