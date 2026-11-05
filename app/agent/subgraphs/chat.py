"""General chat subgraph with memory context and crisis safety."""

from __future__ import annotations

import re

from typing import Any

from app.prompts import load_prompt
from app import secrets_vault
from app.agent.ask_cards import ASK_GUIDE, answer_context, saved_secrets_note, split_reply
from app.agent.harness.guardrails import guard_text
from app.agent.harness.leak import (
    PROMPT_LEAK_REPLY,
    register_allowed_output,
    register_system_prompt,
)
from app.agent.memory import write_event
from app.agent.context_assembler import assemble_context, record_turn
from app.agent.memory.summary import maybe_summarize_turn
from app.agent.subgraphs.profile import load_profile
from app.state import AgentState
from app.agent.crisis_rules import has_crisis_signal
from app.agent.router import current_turn_inputs, intent_result_for
from app.tools.prompt_data import UNTRUSTED_DATA_RULE

PERSONA = load_prompt("chat.persona", "v3")

# Attached domain guidance (text only; grants no tools or permissions).
from app.agent.skills import skills_for_intent, with_skill_context  # noqa: E402

COACH_SKILLS = tuple(item["name"] for item in skills_for_intent("chat"))
CHAT_SYSTEM = with_skill_context(PERSONA, *COACH_SKILLS) if COACH_SKILLS else PERSONA

CRISIS_NOTE = load_prompt("chat.crisis_note", "v1")
HOTLINE_MARK = "010-82951332"
CRISIS_FOOTER = (
    "你现在安全吗？如果有伤害自己的想法，请一定找人说说：北京心理危机研究与干预中心 010-82951332，"
    "或心理援助热线 12356；有紧迫危险请直接打 120 / 110。我也一直在这儿陪你聊。"
)
_ALLOWED_NUMS = {"01082951332", "82951332", "12356", "120", "110"}
_RISK_SYS = load_prompt("chat.risk_classifier", "v1")


register_system_prompt("chat.persona", PERSONA)
register_system_prompt("chat.crisis_note", CRISIS_NOTE)
register_system_prompt("chat.risk_classifier", _RISK_SYS)
register_allowed_output(CRISIS_FOOTER)
register_allowed_output(CRISIS_NOTE[CRISIS_NOTE.index("北京心理危机"):])
# A model may copy a persona example reply verbatim; those replies are safe to send.
for _block in PERSONA.split("<example>")[1:]:
    _example = _block.split("</example>", 1)[0]
    if "回复：" in _example:
        register_allowed_output(_example.split("回复：", 1)[1].strip())
CRISIS_SAFE_BODY = "谢谢你愿意告诉我这些。你现在的感受很重要，我在这儿陪着你，不评判。"

EMPTY_REPLY = "嗨，我在。想聊什么都行——今天的心情、要做的事，或者随便聊聊。要我帮你写点什么、记住什么、设个提醒，直接说就好。"
FALLBACK_REPLY = "我在呢。刚才我这边有点卡，没能好好回你——你愿意再说一遍吗？想聊什么都可以。"


_WEAK_SIGNALS = (
    "累", "难受", "痛苦", "绝望", "崩溃", "没意义", "没意思", "结束", "离开", "睡", "醒",
    "撑", "熬", "算了", "放弃", "解脱", "对不起", "再见", "孤独", "没人", "麻烦",
    "die", "tired", "end", "gone", "hopeless", "sorry",
)


def crisis_latency_cap() -> float:
    """Hard cap (seconds) from message in to crisis reply out. Default 8s."""
    import os

    try:
        return max(2.0, float(os.getenv("CRISIS_LATENCY_CAP_S") or 8))
    except ValueError:
        return 8.0


# Sent first when the model is slow or fails: fixed, human-checked, never generated.
CRISIS_FIXED_BLOCK = CRISIS_FOOTER + "\n\n" + CRISIS_SAFE_BODY


def _llm_risk(msg: str) -> bool:
    """LLM fallback. On timeout/error/garbled output, fail SAFE if any weak signal."""
    import logging
    log = logging.getLogger("crisis")
    out = ""
    try:
        from app.gateway.llm_gateway import chat_text
        out = chat_text(_RISK_SYS, msg[:800], fallback="", timeout=crisis_latency_cap() / 2, task="risk_classify") or ""
    except Exception as e:  # noqa: BLE001
        log.warning("crisis_classifier_error: %s", e)
    up = out.strip().upper()
    if "YES" in up:
        return True
    if "NO" in up:
        return False
    weak = any(w in (msg or "").lower() for w in _WEAK_SIGNALS)
    log.warning("crisis_classifier_unavailable weak_signal=%s chars=%d", weak, len(msg or ""))
    return weak
# Any mention of a hotline / emergency number (the footer carries each exactly once).
_NUMBER_MENTION = re.compile(
    r"(82951332|12356|(?<!\d)1[12]0(?!\d)|号码见下方|热线|急救电话|报警电话|心理援助|危机干预|干预中心)"
)
# A lead-in that only introduces numbers: 「你可以拨打以下电话：」「下面这些热线随时可以打：」.
_LEAD_IN = re.compile(
    r"(以下|下面|如下|这些|这几个|这个|这里有?|可以|随时|请)[^。！？!?\n]{0,16}"
    r"(电话|号码|热线|求助渠道|援助|联系方式|打给|拨打|联系)[^。！？!?\n]{0,10}[：:]?\s*$"
)
# List bullets left behind once their number is gone: 「- 」「1. 」「• 」.
_EMPTY_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.、)）])\s*[：:，,。]?\s*$")


def _sanitize_crisis_reply(reply: str) -> str:
    """Drop model-written numbers and every sentence that mentions one, drop the
    lead-ins they leave dangling, then append the fixed footer once.

    Each hotline / emergency number therefore appears exactly once in the reply."""

    def _keep(m: "re.Match[str]") -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return m.group(0) if digits in _ALLOWED_NUMS else "（号码见下方）"

    body = re.sub(r"\d[\d\- ]{5,}\d", _keep, reply or "")
    body = body.replace(CRISIS_FOOTER, "")
    kept: list[str] = []
    for line in body.split("\n"):
        parts = re.split(r"(?<=[。！？!?；;])", line)
        parts = [p for p in parts if p.strip() and not _NUMBER_MENTION.search(p)]
        kept.append("".join(parts).rstrip())
    # Drop dangling lead-ins / empty bullets, repeatedly from the end of each block.
    out: list[str] = []
    for line in kept:
        if _EMPTY_BULLET.match(line):
            continue
        out.append(line)
    _bullet = re.compile(r"^\s*(?:[-*•·]|\d+[.、)）])\s*\S")
    cleaned: list[str] = []
    for i, line in enumerate(out):
        nxt = next((x for x in out[i + 1:] if x.strip()), "")
        introduces_list = bool(_bullet.match(nxt))  # e.g. 「你可以试试：\n- 深呼吸」 keeps its lead-in
        segs = re.split(r"(?<=[。！？!?；;])", line)
        # The numbers a lead-in introduced are gone: drop the lead-in sentence entirely.
        while segs and segs[-1].strip() and _LEAD_IN.search(segs[-1].strip()) and not introduces_list:
            segs.pop()
        cleaned.append("".join(segs).rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()
    return (body + "\n\n" + CRISIS_FOOTER).strip() if body else CRISIS_FIXED_BLOCK


def _uid(state: AgentState) -> str:
    return state.get("user_id") or "demo"


def _tid(state: AgentState) -> str:
    return str(state.get("thread_id") or "")


def node_chat(state: AgentState) -> dict[str, Any]:
    from app.gateway.llm_gateway import chat_text, has_llm

    uid = _uid(state)
    tid = _tid(state)
    inputs = current_turn_inputs(state)
    msg = str(inputs.get("message") or "").strip()

    import time as _time

    t_start = _time.monotonic()
    system = CHAT_SYSTEM + UNTRUSTED_DATA_RULE
    result = intent_result_for(state, msg)
    crisis = (
        bool(state.get("crisis_detected"))
        or result.rule_id == "crisis"
        or (bool(msg) and _llm_risk(msg))
    )
    if not crisis:
        from app.agent.ask_cards import intent_card

        # Both deterministic and model crisis checks must precede any choice card.
        if result.decision == "clarify" and not state.get("reply_to"):
            card = intent_card(result.candidates)
            return {
                "profile": load_profile(state),
                "messages": [{"role": "assistant", "content": "我先确认一下你的意思。"}],
                "conversation_context": record_turn(state, msg, "我先确认一下你的意思。"),
                "ask": card,
            }
    if crisis:
        system = system + "\n\n" + CRISIS_NOTE
    else:
        system = system + "\n\n" + ASK_GUIDE
        saved = saved_secrets_note(secrets_vault.saved_labels(uid))
        if saved:
            system = system + "\n\n" + saved
    if not crisis:
        from app import db
        from app.agent.memory.profile import remember_name
        from app.agent.context_assembler import conversation_state

        with db.memory_transaction():
            conversation = conversation_state(state)
            turns = conversation["turns"]
            remember_name(uid, msg, thread_id=tid,
                          previous_turn=turns[-1] if turns else None)
            # Only carry context validated before this name update across
            # its revision bump. Other invalidations still discard history.
            conversation["memory_rev"] = int(db.get_profile(uid).get("memory_rev", 0))
        state = {**state, "conversation_context": conversation}
    mem = assemble_context(state, query=msg)
    if mem:
        system += "\n\n" + mem
    if not crisis:
        from app.sports.store import prompt_block as sport_prompt_block

        sport_block = sport_prompt_block(uid)
        if sport_block:
            system += "\n\n" + sport_block
        from app.companion.store import prompt_block as companion_prompt_block

        companion_block = companion_prompt_block(uid)
        if companion_block:
            system += "\n\n" + companion_block
    ask = None
    if not msg:
        reply = EMPTY_REPLY
    else:
        context = answer_context(state.get("reply_to"))
        if context:
            from app.tools.prompt_data import data_block

            context = data_block("memory", context)
        if crisis:
            # Hard latency cap: whatever is left of the budget; on timeout the fixed block goes out.
            left = crisis_latency_cap() - (_time.monotonic() - t_start)
            raw = ""
            if left >= 1.0:
                raw = chat_text(
                    system,
                    f"{context}\n{msg}" if context else msg,
                    task="chat",
                    fallback="",
                    timeout=left,
                ) or ""
            if not raw.strip():
                import logging

                logging.getLogger("crisis").warning("crisis_reply_fixed_block elapsed=%.1fs", _time.monotonic() - t_start)
        else:
            from app.agent.crawl_tools import source_context, CRAWL_TOOLS
            if result.rule_id in {"model_tool:" + name for name in CRAWL_TOOLS}:
                system += source_context(msg, state)
            raw = chat_text(
                system,
                f"{context}\n{msg}" if context else msg,
                task="chat",
                # No key configured: show the offline placeholder (it says how to
                # configure a model) instead of pretending the model was slow.
                fallback=FALLBACK_REPLY if has_llm() else "",
            )
        # Scan before parsing can discard or rewrite leaked prompt text.
        profile = load_profile(state)
        raw = guard_text(raw, user_id=uid, profile=profile, inputs=(msg,), where="chat_raw")
        reply, ask = split_reply(raw)
        # Guard before anything is persisted to memory (summary/episodic feed later prompts).
        reply = guard_text(reply, user_id=uid, profile=profile, inputs=(msg,), where="chat_reply")
        if crisis:
            ask = None
            # The hotline footer is appended after the leak guard so it can never be dropped.
            if reply == PROMPT_LEAK_REPLY:
                reply = ""
            # Empty / timed-out → CRISIS_FIXED_BLOCK (hotline first). Otherwise body + footer once.
            reply = _sanitize_crisis_reply(reply)
        elif not reply and not ask:
            reply = "我在呢。你愿意再多说一点吗？"
    write_event(
        uid,
        "chat",
        ((reply or "") + (f" [问：{ask.get('question') or ask.get('label')}]" if ask else ""))[:240],
        tags=["chat"] + ([f"ask_{ask['type']}"] if ask else []),
        payload={"user_chars": len(msg), "ask_type": ask["type"] if ask else None},
        thread_id=tid,
    )
    if msg:
        maybe_summarize_turn(
            uid,
            kind="chat",
            user_text=msg,
            assistant_text=reply or "",
            thread_id=tid,
        )
    return {
        "conversation_context": record_turn(state, msg, reply or "", compress=not crisis),
        "profile": load_profile(state),
        "messages": [{"role": "assistant", "content": reply}],
        "ask": ask,
    }
