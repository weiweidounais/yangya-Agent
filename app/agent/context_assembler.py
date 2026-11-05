"""Bounded prompt memory and checkpoint-local conversation compression.

A turn is one completed user/assistant exchange. The current request is supplied
separately by the caller. Budgets use the shared token estimator, not a vendor
tokenizer, and cover this context block (not persona/request/output tokens).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.agent.token_meter import estimate_tokens
from app.tools.prompt_data import UNTRUSTED_DATA_RULE, data_block
from app.agent.harness.leak import register_system_prompt

SUMMARY_SYSTEM = (
    "Compress the prior summary and evicted conversation turns into a rolling summary. "
    "Preserve user preferences, corrections, decisions and unresolved questions. "
    "Do not invent facts. Return JSON with a summary string."
) + UNTRUSTED_DATA_RULE
register_system_prompt("context.rolling_summary", SUMMARY_SYSTEM)


@dataclass(frozen=True)
class ContextPolicy:
    raw_turns: int = 4
    token_budget: int = 2400
    summary_tokens: int = 400
    turn_tokens: int = 600
    memory_limit: int = 6

    def __post_init__(self) -> None:
        if min(self.raw_turns, self.token_budget, self.summary_tokens,
               self.turn_tokens, self.memory_limit) < 0:
            raise ValueError("context limits must be non-negative")


def clip(text: str, budget: int) -> str:
    """Largest prefix fitting the estimator, including CJK text."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def memory_context(user_id: str, *, query: str = "", tags=None, limit: int = 6,
                   include_profile: bool = True, token_budget: int = 1200) -> str:
    from app.agent.memory import episodic, profile
    from app.agent.harness.leak import memory_safe

    if not user_id or token_budget <= 0:
        return ""
    from app.feature_flags import memory_vector_enabled
    if memory_vector_enabled():
        from app.agent.memory.retrieval import FACT_TOKEN_CAP, render_facts, retrieve
        from app.data_access import current_user

        uid = current_user()
        if uid is None:
            return ""
        # Reserve room alongside the profile; whole fact entries keep their
        # confirmation labels even under a very small context budget.
        fact_budget = min(FACT_TOKEN_CAP, token_budget // 2 if include_profile else token_budget)
        facts = retrieve(query, limit=min(limit, 8), token_cap=fact_budget)
        material = render_facts(facts)
        blocks = []
        if include_profile:
            blocks.append(clip(memory_safe(profile.format_for_prompt(uid), uid),
                               max(0, token_budget - estimate_tokens(material) - 2)))
        if material:
            blocks.append(material)
        return clip("\n\n".join(blocks), token_budget)
    blocks = [profile.format_for_prompt(user_id)] if include_profile else []
    # No episodic lookup until the caller has a query or explicit tags.
    if limit > 0 and (query.strip() or tags):
        events = episodic.retrieve_events(user_id, query=query, tags=tags, limit=limit)
        if events:
            blocks.append("【相关记忆】\n" + "\n".join(
                f"- [{e['kind']}] {e['summary']}" for e in events if e['summary']
            ))
    return clip(memory_safe("\n\n".join(blocks), user_id), token_budget)


def conversation_state(state: dict[str, Any]) -> dict[str, Any]:
    from app.agent.memory import profile

    uid = str(state.get("user_id") or "")
    if not uid.strip():
        raise ValueError("conversation_state requires user_id")
    tid = str(state.get("thread_id") or "")
    revision = int((profile.get(uid) or {}).get("memory_rev", 0))
    saved = state.get("conversation_context") or {}
    if (saved.get("user_id"), saved.get("thread_id"), saved.get("memory_rev")) != (uid, tid, revision):
        saved = {}
    return {"user_id": uid, "thread_id": tid, "memory_rev": revision,
            "summary": str(saved.get("summary") or ""), "turns": list(saved.get("turns") or [])}


def assemble_context(state: dict[str, Any], *, query: str,
                     policy: ContextPolicy = ContextPolicy()) -> str:
    """Reserve independent space for conversation, the current draft and memory.

    Return an escaped untrusted-data block whose complete rendered size fits
    token_budget. Do not split its markup when clipping oversized inputs.
    """
    from app.agent.harness.leak import memory_safe

    if policy.token_budget <= 0:
        return ""
    saved = conversation_state(state)
    uid = saved["user_id"]
    # Detect intent from the full message; only memory retrieval uses a prefix.
    retrieval_query = query[:200]
    turns = saved["turns"][-policy.raw_turns:] if policy.raw_turns else []
    from app.agent.memory import profile
    from html import escape

    # Independent allocations keep long JDs and profile fields from crowding
    # out the draft and memory. Count escaped text against the budget.
    sections = []
    if turns:
        sections.append((4, "【近期对话（最新在前）】\n" + json.dumps(list(reversed(turns)), ensure_ascii=False)))
    draft = state.get("draft") or {}
    if isinstance(draft, dict) and draft.get("body"):
        sections.append((2, "【当前草稿】\n" + json.dumps(
            {k: draft[k] for k in ("kind", "title", "body") if draft.get(k)}, ensure_ascii=False)))
    if saved["summary"]:
        sections.append((1, "【此前对话摘要】\n" + clip(saved["summary"], policy.summary_tokens)))
    from app.feature_flags import memory_vector_enabled
    if memory_vector_enabled():
        # A large profile must not crowd retrieved facts out of the final block.
        sections.append((1, memory_context(uid, limit=0, token_budget=policy.token_budget)))
        # Defer fact selection until its final weighted allocation is known.
        sections.append((1, None))
    else:
        sections.append((2, memory_context(uid, query=retrieval_query, limit=policy.memory_limit,
                                           token_budget=policy.token_budget)))
    available = max(0, policy.token_budget - estimate_tokens(data_block("memory", "")) - len(sections))
    weight = sum(w for w, _ in sections)
    pieces = []
    for w, text in sections:
        budget = available * w // weight
        if text is None:
            material = memory_safe(memory_context(
                uid, query=retrieval_query, limit=policy.memory_limit,
                include_profile=False, token_budget=budget), uid)
            # Facts are indivisible: never clip away a qualifier or negation.
            if material and estimate_tokens(escape(material, quote=False)) <= budget:
                pieces.append(material)
            continue
        material = memory_safe(text, uid)
        lo, hi = 0, len(material)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if estimate_tokens(escape(material[:mid], quote=False)) <= budget:
                lo = mid
            else:
                hi = mid - 1
        if lo:
            pieces.append(material[:lo])
    return data_block("memory", "\n\n".join(pieces)) if pieces else ""


def record_turn(state: dict[str, Any], user: str, assistant: str, *,
                policy: ContextPolicy = ContextPolicy(), compress: bool = True) -> dict[str, Any]:
    """Return a new checkpoint value; never mutate caller state.

    Failed/offline generation falls back to a bounded extractive summary.
    Only evicted turns are submitted, together with the previous summary.
    """
    from app.agent.harness.leak import memory_safe
    from app.agent.memory.redact import redact_text
    from app.gateway import llm_gateway

    saved = conversation_state(state)
    uid = saved["user_id"]

    def safe(text: str) -> str:
        return memory_safe(redact_text(text), uid)

    turns = saved["turns"] + [{"user": clip(safe(user), policy.turn_tokens),
                                "assistant": clip(safe(assistant), policy.turn_tokens)}]
    split = max(0, len(turns) - policy.raw_turns)
    older, saved["turns"] = turns[:split], turns[split:]
    if older:
        source = saved["summary"] + "\n" + json.dumps(older, ensure_ascii=False)
        result = ""
        if compress and policy.summary_tokens and llm_gateway.has_llm():
            try:
                result = llm_gateway.chat_json(
                    SUMMARY_SYSTEM,
                    data_block("memory", safe(source)), task="summary", fallback={"summary": ""},
                    max_tokens=policy.summary_tokens, timeout=10.0,
                ).get("summary", "")
            except Exception:
                result = ""
        # On failure retain the newly evicted material first; a full prior
        # summary must not permanently crowd out every subsequent update.
        fallback = json.dumps(older, ensure_ascii=False) + "\n" + saved["summary"]
        text = result if isinstance(result, str) and result.strip() else fallback
        saved["summary"] = clip(safe(text), policy.summary_tokens)
    return saved
