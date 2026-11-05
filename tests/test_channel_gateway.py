"""Shared gateway behaviour through mock channels (no network, mock LLM)."""

from __future__ import annotations

import pytest

from app.data_access import user_scope

from app import db
from app.channels import conversation, links
from app.channels.binding import bind_channel, get_binding
from conftest import DRAFT_REQUEST, make_inbound, send, token_from


def _forbid_agent(monkeypatch):
    import app.graph

    def _boom(*a, **k):
        raise AssertionError("agent must not run")

    monkeypatch.setattr(app.graph, "run_action", _boom)


@pytest.mark.parametrize("text", ["帮助", "菜单", "?", "？", "help", "/help"])
def test_help_messages_follow_model_selection(mock_text_channel, uid, monkeypatch, text):
    import json
    from unittest.mock import Mock
    import app.graph
    from app.gateway import llm_gateway as gw

    bind_channel("mock_text", uid, uid)
    model = Mock(return_value={"tool": None, "confidence": 0.95})
    monkeypatch.setattr(gw, "has_llm", lambda *a: True)
    monkeypatch.setattr(gw, "chat_json", model)
    agent = Mock(return_value={"thread_id": "help-thread", "state": {
        "messages": [{"role": "assistant", "content": "模型生成的帮助说明"}],
    }})
    monkeypatch.setattr(app.graph, "run_action", agent)
    out = send("mock_text", uid, text)
    assert out["status"] == "chat"
    assert out["reply_struct"]["text"] == "模型生成的帮助说明"
    model.assert_called_once()
    assert json.loads(model.call_args.args[1])["message"] == text
    agent.assert_called_once()
    payload = agent.call_args.args[0]
    assert payload["action"] == "chat" and payload["message"] == text
    assert payload["intent_result"]["rule_id"] == "model_tool:none"


@pytest.mark.parametrize("text", ["帮助", "菜单", "?"])
def test_unbound_help_keeps_identity_gate(mock_text_channel, monkeypatch, text):
    _forbid_agent(monkeypatch)
    assert send("mock_text", "unbound-help", text)["status"] == "need_bind"


def test_unbound_user_only_gets_bind_link(mock_text_channel, monkeypatch):
    _forbid_agent(monkeypatch)
    out = send("mock_text", "wx_new", "帮我写一封邮件")
    assert out["status"] == "need_bind"
    assert out["outbound"]["user_id"] is None
    link = out["reply_struct"]["links"][0]["url"]
    assert link.startswith("https://agent.example.test/bind?t=")
    assert links.peek(token_from(link), "bind")["external_user_id"] == "wx_new"
    assert "回复数字选择" not in out["outbound"]["text"]
    with user_scope("demo"):
        assert db.get_profile("demo") == {}


def test_reserved_demo_account_is_never_used(mock_text_channel, monkeypatch):
    with pytest.raises(ValueError):
        bind_channel("mock_text", "wx_demo", "demo")
    _forbid_agent(monkeypatch)
    parsed = make_inbound("mock_text", "wx_demo", "你好")
    parsed["inbound"]["user_id"] = "demo"
    parsed["inbound"]["bound"] = True
    from app.channels.gateway import handle_inbound

    assert handle_inbound(parsed, "mock_text")["status"] == "need_bind"


def test_bind_command_reissues_link_and_revokes_old(mock_text_channel):
    first = send("mock_text", "wx_re", "你好")["reply_struct"]["links"][0]["url"]
    second = send("mock_text", "wx_re", "绑定")
    assert second["status"] == "bind_link"
    assert links.peek(token_from(first), "bind") is None
    assert links.peek(token_from(second["reply_struct"]["links"][0]["url"]), "bind")


def test_group_chat_blocked_without_bind_link(mock_text_channel):
    out = send("mock_text", "wx_grp", "你好", is_group=True)
    assert out["status"] == "group_blocked"
    assert not out["reply_struct"]["links"]


def test_pii_message_is_not_processed(mock_text_channel, monkeypatch):
    bind_channel("mock_text", "wx_pii", "u-pii")
    _forbid_agent(monkeypatch)
    out = send("mock_text", "wx_pii", "我的手机号是 13812345678，邮箱 zs@example.com")
    assert out["status"] == "privacy_redirect"
    assert set(out["pii"]) == {"mobile", "email"}
    assert "13812345678" not in str(out)
    assert "zs@example.com" not in str(out)
    assert "/sandbox?t=" in out["outbound"]["text"]


def test_credential_message_is_not_processed(mock_text_channel, monkeypatch):
    bind_channel("mock_text", "wx_pw", "u-pw")
    _forbid_agent(monkeypatch)
    out = send("mock_text", "wx_pw", "记一下，我的邮箱密码是 hunter2abc")
    assert out["status"] == "privacy_redirect"
    assert out["reply_struct"]["kind"] == "privacy"
    assert "hunter2abc" not in str(out)


def test_draft_confirm_by_number_on_text_channel(mock_text_channel):
    bind_channel("mock_text", "wx_flow", "u-flow")
    out = send("mock_text", "wx_flow", DRAFT_REQUEST)
    assert out["status"] == "awaiting_confirm"
    text = out["outbound"]["text"]
    assert "1. 确认" in text and "2. 拒绝" in text
    assert "在网页查看完整草稿：https://agent.example.test/sandbox?t=" in text
    tid = out["thread_id"]
    assert conversation.get_pending("mock_text", "wx_flow")["thread_id"] == tid

    done = send("mock_text", "wx_flow", "1")
    assert done["status"] == "confirmed", done
    assert done["thread_id"] == tid
    assert conversation.get_pending("mock_text", "wx_flow") == {}
    assert "已确认" in done["outbound"]["text"]


def test_reject_by_keyword_uses_pending_thread(mock_text_channel):
    bind_channel("mock_text", "wx_rej", "u-rej")
    tid = send("mock_text", "wx_rej", DRAFT_REQUEST)["thread_id"]
    out = send("mock_text", "wx_rej", "拒绝")
    assert out["thread_id"] == tid
    assert out["status"] in ("rejected", "awaiting_confirm")


def test_card_click_confirms(mock_card_channel):
    bind_channel("mock_card", "ou_card", "u-card")
    out = send("mock_card", "ou_card", DRAFT_REQUEST)
    card = out["outbound"]["card"]
    buttons = [a for el in card["elements"] if el["tag"] == "action" for a in el["actions"]]
    confirm = next(b for b in buttons if b.get("value", {}).get("command") == "CONFIRM")
    assert confirm["value"]["thread_id"] == out["thread_id"]
    assert any(b.get("url", "").startswith("https://agent.example.test/sandbox?t=") for b in buttons)

    clicked = send("mock_card", "ou_card", "CONFIRM", action_value=confirm["value"])
    assert clicked["status"] == "confirmed"


def test_unbind_flow(mock_text_channel):
    bind_channel("mock_text", "wx_ub", "u-ub")
    ask = send("mock_text", "wx_ub", "解绑")
    assert ask["status"] == "unbind_confirm"
    assert send("mock_text", "wx_ub", "1")["status"] == "unbound"
    assert get_binding("mock_text", "wx_ub") is None
    assert send("mock_text", "wx_ub", "你好")["status"] == "need_bind"


def test_whoami_masks_user_id(mock_text_channel):
    bind_channel("mock_text", "wx_who", "user-123456")
    out = send("mock_text", "wx_who", "我是谁")
    assert out["status"] == "already_bound"
    assert "user-123456" not in out["outbound"]["text"]


def test_reply_asking_for_contact_gets_sandbox_link(mock_text_channel, monkeypatch):
    from app.channels import gateway
    from app.channels.base import ChannelReply

    bind_channel("mock_text", "wx_ask", "u-ask")
    monkeypatch.setattr(
        gateway,
        "_do_agent",
        lambda ctx, intent, text: (ChannelReply(text="可以把手机号发我一下"), intent, {"intent": intent}),
    )
    out = send("mock_text", "wx_ask", "最近好焦虑")
    assert "网页安全通道" in out["outbound"]["text"]
    assert "/sandbox?t=" in out["outbound"]["text"]


def test_chat_shares_one_agent_thread_across_channels(mock_text_channel, mock_card_channel, monkeypatch):
    import app.graph

    bind_channel("mock_text", "wx_same", "u-same")
    bind_channel("mock_card", "ou_same", "u-same")
    threads: list[str] = []
    real = app.graph.run_action

    def spy(payload, *, thread_id=None):
        threads.append(thread_id)
        return real(payload, thread_id=thread_id)

    monkeypatch.setattr(app.graph, "run_action", spy)
    send("mock_text", "wx_same", "今天有点累")
    send("mock_card", "ou_same", "随便聊聊吧")
    assert len(threads) == 2 and threads[0] == threads[1] == conversation.conversation_thread_id("u-same")


@pytest.fixture(autouse=True)
def _trusted_test_setup():
    from app.data_access import system_scope
    with system_scope():
        yield
