"""HTTP surface: bind page, sandbox, switches, WeChat webhook, adapters (mocked network)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app.data_access import user_scope

from app import db
from app.channels import links
from app.channels.binding import bind_channel, get_binding
from app.channels.registry import get_adapter
from conftest import DRAFT_REQUEST, RawTestClient, send, token_from


def _bind_link(channel: str, external: str) -> str:
    return token_from(send(channel, external, "你好")["reply_struct"]["links"][0]["url"])


# ----- conversational bind via web page -----


@pytest.fixture
def browser():
    """Real cookie-based browser session (no auto-signed test identity)."""
    from app.main import app

    with RawTestClient(app) as c:
        yield c


def _web_session(client) -> str:
    r = client.post("/api/session")
    assert r.status_code == 200, r.text
    return r.json()["data"]["user_id"]


def test_bind_page_flow(browser, mock_text_channel):
    token = _bind_link("mock_text", "wx_web")
    info = browser.get("/api/channels/link", params={"t": token, "purpose": "bind"})
    assert info.status_code == 200 and info.json()["channel"] == "mock_text"
    assert browser.get("/bind").status_code == 200

    uid = _web_session(browser)
    r = browser.post("/api/channels/bind/complete", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] != uid and "*" in r.json()["user_id"]
    assert get_binding("mock_text", "wx_web")["user_id"] == uid
    assert "绑定成功" in mock_text_channel.sent[-1]["text"]
    assert uid not in mock_text_channel.sent[-1]["text"]

    again = browser.post("/api/channels/bind/complete", json={"token": token})
    assert again.status_code == 410


def test_bind_requires_web_session_and_keeps_link(browser, mock_text_channel):
    token = _bind_link("mock_text", "wx_nosession")
    r = browser.post("/api/channels/bind/complete", json={"token": token})
    assert r.status_code == 401
    assert get_binding("mock_text", "wx_nosession") is None
    # The link was not consumed: the same page works once a session exists.
    uid = _web_session(browser)
    assert browser.post("/api/channels/bind/complete", json={"token": token}).status_code == 200
    assert get_binding("mock_text", "wx_nosession")["user_id"] == uid


def test_bind_rejects_reserved_demo_identity(browser, mock_text_channel):
    from app.auth import mint_user_token
    from app.session_identity import SESSION_COOKIE

    token = _bind_link("mock_text", "wx_d")
    browser.cookies.set(SESSION_COOKIE, mint_user_token("demo"))
    r = browser.post("/api/channels/bind/complete", json={"token": token})
    assert r.status_code == 400
    assert get_binding("mock_text", "wx_d") is None


# ----- privacy sandbox -----


def test_sandbox_submit_updates_profile_without_leaking(client, mock_text_channel):
    bind_channel("mock_text", "wx_sb", "u-sb")
    out = send("mock_text", "wx_sb", "隐私")
    assert out["status"] == "sandbox_link"
    token = token_from(out["reply_struct"]["links"][0]["url"])
    assert client.get("/sandbox").status_code == 200

    bad = client.post("/api/sandbox/submit", json={"token": token, "phone": "123"})
    assert bad.status_code == 400
    r = client.post(
        "/api/sandbox/submit",
        json={"token": token, "name": "张三", "city": "上海", "phone": "13812345678", "email": "zs@example.com",
              "facts": "喜欢晨跑\n下周去杭州"},
    )
    assert r.status_code == 200, r.text
    with user_scope("u-sb"):
        profile = db.get_profile("u-sb")
    assert profile["phone"] == "13812345678" and profile["email"] == "zs@example.com"
    assert profile["name"] == "张三" and profile["city"] == "上海"
    assert profile.get("facts") == ["喜欢晨跑", "下周去杭州"]
    note = mock_text_channel.sent[-1]["text"]
    assert "13812345678" not in note and "zs@example.com" not in note
    assert client.post("/api/sandbox/submit", json={"token": token, "phone": "13812345678"}).status_code == 410


def test_draft_link_shows_full_draft_on_web(client, mock_text_channel):
    bind_channel("mock_text", "wx_dv", "u-dv")
    out = send("mock_text", "wx_dv", DRAFT_REQUEST)
    assert out["status"] == "awaiting_confirm"
    url = next(l["url"] for l in out["reply_struct"]["links"] if "sandbox" in l["url"])
    r = client.get("/api/sandbox/draft", params={"t": token_from(url)})
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == out["thread_id"] and body["body"] and body["kind"] == "email"
    # The channel only carries a preview; the web page has the whole draft.
    assert len(body["body"]) > 0 and "在网页查看完整草稿" in out["outbound"]["text"]


# ----- switches default OFF -----


def test_channels_default_off(client, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    from app.channels import feishu_ws

    assert not get_adapter("feishu").enabled()
    assert not feishu_ws.ws_enabled()
    assert feishu_ws.start_background()["started"] is False
    assert not get_adapter("openclaw_wechat").enabled()
    assert client.post("/webhooks/openclaw/wechat", json={}).status_code == 503
    assert client.post("/webhooks/feishu", json={}).status_code == 503
    health = client.get("/health").json()
    assert health["channels"]["feishu"]["enabled"] is False
    assert health["channels"]["openclaw_wechat"]["enabled"] is False
    assert health["channels"]["qq"]["reserved"] is True


def test_feishu_switch_on(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    monkeypatch.setenv("FEISHU_ENABLED", "1")
    from app.channels import feishu_ws

    assert get_adapter("feishu").enabled() and feishu_ws.ws_enabled()


def test_wechat_requires_webhook_token(client, monkeypatch):
    monkeypatch.setenv("OPENCLAW_WECHAT_ENABLED", "1")
    assert client.post("/webhooks/openclaw/wechat", json={}).status_code == 503
    monkeypatch.setenv("OPENCLAW_WEBHOOK_TOKEN", "tok")
    assert client.post("/webhooks/openclaw/wechat", json={}).status_code == 401
    assert (
        client.post("/webhooks/openclaw/wechat", json={}, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


# ----- WeChat via OpenClaw bridge -----


def _wechat_on(monkeypatch, *, push: bool = False):
    monkeypatch.setenv("OPENCLAW_WECHAT_ENABLED", "1")
    monkeypatch.setenv("OPENCLAW_WEBHOOK_TOKEN", "tok")
    if push:
        monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://gw.local:18789")
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gw-secret")


def _plugin_payload(sender: str, text: str, **extra):
    """Shape posted by integrations/openclaw-personal-agent-bridge."""
    return {
        "channel": "openclaw-weixin",
        "from": sender,
        "sender_name": "张三",
        "text": text,
        "msg_id": f"m-{time.time_ns()}",
        "conversation_id": sender,
        "account_id": "acc1",
        "is_group": False,
        **extra,
    }


AUTH = {"Authorization": "Bearer tok"}


def test_wechat_unbound_gets_bind_link_sync(client, monkeypatch):
    _wechat_on(monkeypatch)
    r = client.post("/webhooks/openclaw/wechat", json=_plugin_payload("wxid_new", "你好"), headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "need_bind"
    assert "打开网页完成绑定：https://agent.example.test/bind?t=" in body["reply"]["text"]
    token = token_from(body["reply"]["text"].split("：")[-1])
    assert links.peek(token, "bind")["display_name"] == "张三"


def test_wechat_full_flow_sync_numbered_options(client, monkeypatch):
    _wechat_on(monkeypatch)
    bind_channel("openclaw_wechat", "wxid_flow", "u-wx")
    r = client.post("/webhooks/openclaw/wechat", json=_plugin_payload("wxid_flow", DRAFT_REQUEST), headers=AUTH)
    body = r.json()
    assert body["status"] == "awaiting_confirm"
    assert "1. 确认" in body["reply"]["text"]
    r2 = client.post("/webhooks/openclaw/wechat", json=_plugin_payload("wxid_flow", "1"), headers=AUTH)
    assert r2.json()["status"] == "confirmed"


def test_wechat_async_push_via_gateway(client, monkeypatch):
    from app.channels import openclaw_wechat as oc

    _wechat_on(monkeypatch, push=True)
    bind_channel("openclaw_wechat", "wxid_async", "u-async")
    pushed = []
    monkeypatch.setattr(oc, "send_via_gateway", lambda **kw: pushed.append(kw) or {"ok": True})
    import app.channels.gateway as gw

    real = gw._do_draft

    def slow(ctx, text):
        time.sleep(0.5)
        return real(ctx, text)

    monkeypatch.setattr(gw, "_do_draft", slow)
    r = client.post(
        "/webhooks/openclaw/wechat",
        json=_plugin_payload("wxid_async", DRAFT_REQUEST, sync_wait_seconds=0.05),
        headers=AUTH,
    )
    assert r.json()["status"] == "accepted_async"
    for _ in range(100):
        if pushed:
            break
        time.sleep(0.05)
    assert pushed and pushed[0]["to"] == "wxid_async" and pushed[0]["account_id"] == "acc1"
    assert "1. 确认" in pushed[0]["text"]


def test_openclaw_tools_invoke_payload(monkeypatch):
    from app.channels import openclaw_wechat as oc

    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "http://gw.local:18789")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gw-secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {}})

    real_client = httpx.Client
    monkeypatch.setattr(oc.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    res = oc.send_via_gateway(to="wxid_1", text="你好", account_id="acc1")
    assert res["ok"] is True
    assert seen["url"] == "http://gw.local:18789/tools/invoke"
    assert seen["auth"] == "Bearer gw-secret"
    assert seen["body"]["tool"] == "message" and seen["body"]["action"] == "send"
    assert seen["body"]["args"] == {
        "channel": "openclaw-weixin",
        "target": "wxid_1",
        "message": "你好",
        "accountId": "acc1",
    }


# ----- Feishu adapter -----


def test_feishu_card_action_parse_and_send(monkeypatch):
    from app.channels import feishu_client
    from app.channels.feishu import parse_feishu_webhook
    from app.channels.gateway import handle_inbound

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "s")
    monkeypatch.setenv("FEISHU_ENABLED", "1")
    sent = []
    monkeypatch.setattr(feishu_client, "deliver_outbound", lambda **kw: sent.append(kw) or {"ok": True})
    bind_channel("feishu", "ou_fs", "u-fs")

    msg = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_fs"}, "sender_type": "user"},
            "message": {"message_id": "om_1", "chat_type": "p2p", "content": json.dumps({"text": DRAFT_REQUEST})},
        },
    }
    from app.channels.gateway import deliver_result

    parsed = parse_feishu_webhook(msg, skip_auth=True)
    result = deliver_result("feishu", parsed["inbound"], handle_inbound(parsed, "feishu"))
    assert result["status"] == "awaiting_confirm"
    card = sent[-1]["card"]
    assert sent[-1]["message_id"] == "om_1" and card["header"]["template"] == "orange"
    confirm = next(
        a for el in card["elements"] if el["tag"] == "action" for a in el["actions"]
        if a.get("value", {}).get("command") == "CONFIRM"
    )

    click = parse_feishu_webhook(
        {"action": {"value": confirm["value"]}, "operator": {"open_id": "ou_fs"}, "open_message_id": "om_card"},
        skip_auth=True,
    )
    assert click["kind"] == "card_action" and click["inbound"]["action_value"]["opt"] == "confirm"
    out = deliver_result("feishu", click["inbound"], handle_inbound(click, "feishu"))
    assert out["status"] == "confirmed"
    assert sent[-1]["message_id"] == "om_card"


# ----- admin API / reserved channel / identity / privacy units -----


def test_admin_bind_api_requires_token(client, monkeypatch):
    body = {"channel": "feishu", "external_user_id": "ou_adm", "user_id": "u-adm"}
    assert client.post("/api/channels/bind", json=body).status_code == 403
    monkeypatch.setenv("CHANNEL_ADMIN_TOKEN", "adm")
    assert client.post("/api/channels/bind", json=body, headers={"X-Admin-Token": "x"}).status_code == 401
    assert client.post("/api/channels/bind", json=body, headers={"X-Admin-Token": "adm"}).status_code == 200
    demo = {**body, "user_id": "demo"}
    assert client.post("/api/channels/bind", json=demo, headers={"X-Admin-Token": "adm"}).status_code == 400


def test_qq_is_reserved_not_implemented():
    from app.channels.base import ChannelReply
    from app.channels.outbound import send_reply

    qq = get_adapter("qq")
    assert qq.reserved and not qq.enabled()
    with pytest.raises(NotImplementedError):
        qq.parse_inbound({})
    assert send_reply("qq", "123", ChannelReply(text="hi"))["skipped"] is True


def test_privacy_detect_and_redact():
    from app.channels.privacy import detect_pii, redact

    text = "电话 138-1234-5678，身份证 11010519491231002X，卡号 4111 1111 1111 1111，邮箱 a.b@c.com"
    assert set(detect_pii(text)) == {"mobile", "id_card", "email", "bank_card"}
    red = redact(text)
    for secret in ("138-1234-5678", "11010519491231002X", "4111 1111 1111 1111", "a.b@c.com"):
        assert secret not in red
    hotline = "北京心理危机研究与干预中心 010-82951332，或 12356"
    assert detect_pii(hotline) == [] and redact(hotline) == hotline
    assert detect_pii("薪资 30-40k，5年经验，订单号 20240101") == []


def test_static_pages_use_design_system_and_no_pre():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "static"
    for name in ("bind.html", "sandbox.html"):
        html = (root / name).read_text(encoding="utf-8")
        assert "/static/design.css" in html and "btn btn-primary" in html
        assert "<pre" not in html
    assert 'class="draft-body"' in (root / "sandbox.html").read_text(encoding="utf-8")
    assert "maskId(data.user_id)" in (root / "bind.html").read_text(encoding="utf-8")


def test_mask_never_returns_raw_short_id():
    from app.channels.gateway import _mask

    assert _mask("abc") == "****" and "uid-12345" != _mask("uid-12345")


@pytest.fixture(autouse=True)
def _trusted_channel_setup():
    from app.data_access import system_scope
    with system_scope():
        yield
