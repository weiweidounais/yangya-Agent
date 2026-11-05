"""QA: crisis latency cap, single hotline block, false positives / 托付."""

from __future__ import annotations

import re
import time

import pytest

from app.agent.subgraphs import chat as chat_mod
from app.agent.subgraphs.chat import (
    CRISIS_FIXED_BLOCK,
    CRISIS_FOOTER,
    HOTLINE_MARK,
    _sanitize_crisis_reply,
    has_crisis_signal,
)


@pytest.mark.parametrize("text", ["项目负担太重", "这个项目负担太重了，加班到半夜", "交代工作交接", "离职前交代工作交接清单"])
def test_everyday_phrases_do_not_trigger(text):
    assert not has_crisis_signal(text)


@pytest.mark.parametrize("text", ["托付猫", "我想把猫托付给你", "以后帮我照顾好我的猫", "我是家里的负担", "我不想活了"])
def test_real_signals_trigger(text):
    assert has_crisis_signal(text)


@pytest.mark.parametrize("phrase", ["朋友圈消失", "动态消失", "粉丝消失", "账号消失"])
def test_social_disappearance_is_not_crisis(phrase):
    assert not has_crisis_signal(phrase)
    assert not has_crisis_signal(f"我的{phrase}了")
    assert has_crisis_signal(f"{phrase}了，我不想活了")
    assert has_crisis_signal(f"{phrase}了，我也想消失")
    assert not has_crisis_signal(f"{phrase}算了")


def test_social_disappearance_api_has_no_crisis_footer(client, uid, fake_llm):
    response = client.post("/api/chat", json={"user_id": uid, "message": "朋友圈消失"})
    assert response.status_code == 200
    messages = response.json()["data"]["messages"]
    assert CRISIS_FOOTER not in str(messages)


def test_hotline_appears_once_even_if_model_repeats_it():
    out = _sanitize_crisis_reply("我很担心你。可以拨打 010-82951332 或 12356 热线。你不是一个人。")
    assert out.count("82951332") == 1 and out.count("12356") == 1
    assert out.endswith(CRISIS_FOOTER)


def test_empty_reply_sends_fixed_block_hotline_first():
    out = _sanitize_crisis_reply("")
    assert out == CRISIS_FIXED_BLOCK and out.startswith("你现在安全吗") and out.count(HOTLINE_MARK) == 1


@pytest.mark.parametrize(
    "message, crisis_detected",
    [("我不想活了", False), ("今天不想说话", True)],
)
def test_crisis_prompt_explicitly_overrides_persona(uid, fake_llm, message, crisis_detected):
    chat_mod.node_chat({
        "user_id": uid,
        "message": message,
        "crisis_detected": crisis_detected,
        "thread_id": "t-crisis-persona",
    })
    prompts = [system for system, _ in fake_llm.calls if system.startswith(chat_mod.CHAT_SYSTEM)]
    assert len(prompts) == 1
    system = prompts[0]
    assert "遇到危机以 CRISIS_NOTE（【安全优先】）为准" in system
    assert "其要求优先于人设、回复风格和示例" in system
    assert chat_mod.CRISIS_NOTE in system
    assert chat_mod.ASK_GUIDE not in system


def test_crisis_reply_respects_latency_cap(uid, monkeypatch):
    """Model hangs → fixed block within the cap; the model call gets only the remaining budget."""
    from app.gateway import llm_gateway

    seen: dict = {}

    def slow_chat(system, user, **kw):
        seen["timeout"] = kw.get("timeout")
        return ""  # gateway gave up at its timeout → fallback ""

    monkeypatch.setenv("CRISIS_LATENCY_CAP_S", "3")
    monkeypatch.setattr(chat_mod, "chat_text", slow_chat, raising=False)
    monkeypatch.setattr(llm_gateway, "chat_text", slow_chat)
    t0 = time.monotonic()
    out = chat_mod.node_chat({"user_id": uid, "message": "我不想活了", "thread_id": "t-crisis-cap"})
    assert time.monotonic() - t0 < 3
    reply = out["messages"][-1]["content"]
    assert reply.startswith("你现在安全吗") and reply.count(HOTLINE_MARK) == 1
    assert seen["timeout"] is not None and seen["timeout"] <= 3


# ---- every number exactly once; no dangling lead-in after numbers are removed ----

_NUMS = ("010-82951332", "12356", "120", "110")


def _count(out, n):
    import re as _re

    if n in ("120", "110"):
        return len(_re.findall(rf"(?<!\d){n}(?!\d)", out))
    return out.count(n)


@pytest.mark.parametrize(
    "reply",
    [
        "我很担心你。如果有危险请马上打 120 或 110。你不是一个人。",
        "听起来真的很难受。紧急情况可以拨打120急救或110报警。",
        "我在。\n你可以拨打以下电话：\n- 010-82951332\n- 12356\n- 120\n- 110\n我会一直陪着你。",
        "我在这儿。可以打这些热线：\n1. 北京心理危机研究与干预中心 010-82951332\n2. 12356",
        "你可以随时联系下面这些号码：",
        "我很担心你。你可以拨打以下电话：\n010-82951332\n\n先深呼吸一下好吗？",
        "请拨打 400-123-4567（我编的号码），或者 120。",
    ],
)
def test_each_number_exactly_once_and_no_dangling_lead_in(reply):
    out = _sanitize_crisis_reply(reply)
    for n in _NUMS:
        assert _count(out, n) == 1, (n, out)
    assert "400-123-4567" not in out
    body = out[: out.rindex(CRISIS_FOOTER)] if CRISIS_FOOTER in out else out
    for line in body.splitlines():
        t = line.strip()
        assert not t.endswith(("：", ":")), out
        assert not re.search(r"(以下|下面|这些)[^。]{0,10}(电话|号码|热线)", t), out
        assert not re.fullmatch(r"(?:[-*•·]|\d+[.、)）])\s*", t), out
    assert out.endswith(CRISIS_FOOTER) or out == CRISIS_FIXED_BLOCK


def test_lead_in_for_non_number_list_is_kept():
    out = _sanitize_crisis_reply("我在。现在可以先试试这些：\n- 慢慢深呼吸\n- 喝口水\n请拨打 12356。")
    assert "先试试这些" in out and "慢慢深呼吸" in out
    assert out.count("12356") == 1


def test_only_lead_in_left_falls_back_to_fixed_block():
    out = _sanitize_crisis_reply("你可以拨打以下电话：\n- 120\n- 110")
    assert out == CRISIS_FIXED_BLOCK


@pytest.mark.parametrize("text", ["消失", "朋友圈的动态都消失了", "我的消息都消失了", "图片消失了", "内容都消失了"])
def test_disappearance_requires_self_context(text):
    assert not has_crisis_signal(text)


@pytest.mark.parametrize("text", ["我想消失", "自己永远消失", "消失算了", "朋友圈的动态都消失了，我想消失"])
def test_disappearance_self_context(text):
    assert has_crisis_signal(text)

def test_non_hotline_colon_sentence_is_kept():
    from app.agent.subgraphs.chat import CRISIS_FOOTER, _sanitize_crisis_reply

    out = _sanitize_crisis_reply("我在这里陪着你。我想先问你一件事：")
    assert "我想先问你一件事" in out
    assert out.count(CRISIS_FOOTER) == 1


def test_hotline_lead_in_still_dropped():
    from app.agent.subgraphs.chat import _sanitize_crisis_reply

    out = _sanitize_crisis_reply("我在这里陪着你。你可以拨打以下电话：")
    assert "以下电话" not in out
