"""Harness guardrails for the Personal Agent.

1) No leaks in outbound text: secrets, system prompts, other users, privacy sandbox
2) Group chat never receives personal data
"""

from __future__ import annotations

from typing import Any

from app.agent.harness import context as trace_ctx
from app.agent.harness.leak import PROMPT_LEAK_REPLY, scan_output

_OUTPUT_TREES = ("draft", "ask")


def guard_node_output(
    result: dict[str, Any],
    *,
    user_id: str,
    profile: dict[str, Any] | None = None,
    inputs: tuple[str, ...] = (),
    sandbox: tuple[str, ...] | list[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply the leak guard to every user-visible text in a node result.

    Strict: any failure to load the data the scan depends on raises, so the
    caller can fail closed instead of emitting a partially-checked result.
    """
    hits: list[dict[str, Any]] = []
    out = dict(result or {})
    if profile is None:
        from app import db

        # Nodes can carry current-user facts not yet persisted. Saved edits win
        # over checkpoint values, as they do when loading the chat profile.
        profile = {**(out.get("profile") or {}),
                   **(db.get_profile(user_id) if user_id else {})}

    def _scan(text: Any, where: str) -> Any:
        if not isinstance(text, str) or not text:
            return text
        res = scan_output(
            text, user_id=user_id, profile=profile, inputs=inputs, sandbox=sandbox, strict=True
        )
        for h in res["hits"]:
            hits.append({**h, "where": where, "blocked": res["blocked"]})
        return res["text"]

    msgs = out.get("messages")
    if isinstance(msgs, list):
        new_msgs = []
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role", "assistant") == "assistant":
                m = {**m, "content": _scan(m.get("content"), f"messages[{i}]")}
            new_msgs.append(m)
        out["messages"] = new_msgs

    def _tree(v: Any, where: str, depth: int = 0) -> Any:
        if isinstance(v, str):
            return _scan(v, where)
        if depth > 4:
            return v
        if isinstance(v, list):
            return [_tree(x, f"{where}[]", depth + 1) for x in v]
        if isinstance(v, dict):
            return {
                k: _tree(x, f"{where}.{k}", depth + 1)
                for k, x in v.items()
            }
        return v

    for key in _OUTPUT_TREES:
        if isinstance(out.get(key), dict):
            out[key] = _tree(out[key], key, depth=-2 if key == "ask" else 0)
    for key in ("next_suggestion", "error"):
        if isinstance(out.get(key), str):
            out[key] = _scan(out[key], key)
    for h in hits:
        trace_ctx.record_guardrail({"guardrail": "no_leak", **h})
    return out, hits


def guard_text(text: str, *, user_id: str, inputs: tuple[str, ...] = (), where: str = "text",
               profile: dict[str, Any] | None = None) -> str:
    """Leak-guard one string and record hits on the current trace step."""
    try:
        res = scan_output(text or "", user_id=user_id, profile=profile, inputs=inputs, strict=True)
    except Exception:
        res = {"text": PROMPT_LEAK_REPLY, "hits": [{"type": "guard_error", "count": 1}], "blocked": True}
    for h in res["hits"]:
        trace_ctx.record_guardrail({"guardrail": "no_leak", **h, "where": where, "blocked": res["blocked"]})
    return res["text"]


def guard_outbound_text(text: str, *, user_id: str, inputs: tuple[str, ...] = ()) -> str:
    """Last-mile leak guard for channel delivery (IM)."""
    return guard_text(text, user_id=user_id, inputs=inputs, where="outbound")


def group_chat_blocks_personal_data(is_group: bool) -> bool:
    """True when outbound must not include personal data."""
    return bool(is_group)
