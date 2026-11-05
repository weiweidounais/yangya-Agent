from __future__ import annotations

import json
import logging

import pytest

PASSWORD = "Pa55-w0rd!Sandbox"
COOKIE = "__zp_stoken__=abcdefgh12345678; wt2=zzzzzzzzyyyyyy; geek_zp_token=qqqqqqqqwwwwwww"
PHONE = "13987654321"
ID_CARD = "11010519491231002X"
from app.data_access import user_scope


PLATFORM = "网盘"
LOGIN_VALUES = {"platform_name": PLATFORM, "account": PHONE, "password": PASSWORD}


def _save(client, user_id, key="account_login", values=None):
    return client.post(
        "/api/secrets",
        json={"user_id": user_id, "key": key, "purpose": "查看消息", "values": values or dict(LOGIN_VALUES)},
    )


def test_save_list_delete(client, vault_key):
    r = _save(client, "api-user-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["message"] == "已安全保存 账号登录信息"
    assert PASSWORD not in r.text and PHONE not in r.text

    r = client.get("/api/secrets", params={"user_id": "api-user-1"})
    items = r.json()["data"]["items"]
    assert [i["key"] for i in items] == ["account_login"]
    assert {m["preview"] for m in items[0]["masked"]} == {PLATFORM, "139****4321", "已保存"}
    assert PASSWORD not in r.text and PHONE not in r.text

    assert client.delete("/api/secrets/account_login", params={"user_id": "api-user-1"}).status_code == 200
    assert client.get("/api/secrets", params={"user_id": "api-user-1"}).json()["data"]["items"] == []
    assert client.delete("/api/secrets/account_login", params={"user_id": "api-user-1"}).status_code == 404


def test_endpoint_isolation(client, vault_key):
    assert _save(client, "iso-alice").status_code == 200
    assert client.get("/api/secrets", params={"user_id": "iso-bob"}).json()["data"]["items"] == []
    assert client.delete("/api/secrets/account_login", params={"user_id": "iso-bob"}).status_code == 404
    items = client.get("/api/secrets", params={"user_id": "iso-alice"}).json()["data"]["items"]
    assert len(items) == 1


@pytest.mark.parametrize("uid", ["", "demo", "  "])
def test_requires_real_account(client, vault_key, uid):
    assert _save(client, uid).status_code == 401
    assert client.get("/api/secrets", params={"user_id": uid}).status_code == 401
    assert client.delete("/api/secrets/account_login", params={"user_id": uid}).status_code == 401


def test_demo_allowed_only_with_explicit_flag(client, vault_key, monkeypatch):
    monkeypatch.setenv("SECRETS_ALLOW_DEMO", "1")
    assert _save(client, "demo").status_code == 200
    client.delete("/api/secrets/account_login", params={"user_id": "demo"})


def test_vault_unconfigured_returns_503(client, monkeypatch):
    monkeypatch.delenv("SECRETS_FERNET_KEY", raising=False)
    r = _save(client, "nokey-user")
    assert r.status_code == 503
    assert PASSWORD not in r.text
    assert client.get("/api/health").json()["secrets_vault"]["configured"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "bad-1", "key": "account_login", "values": {"platform_name": PLATFORM, "account": PASSWORD}},
        {"user_id": "bad-1", "key": "account_login", "values": {"account": PHONE, "password": PASSWORD}},
        {"user_id": "bad-1", "key": "phone", "values": {"phone": PASSWORD}},
        {"user_id": "bad-1", "key": "nope", "values": {"x": PASSWORD}},
        {"user_id": "bad-1", "key": "account_login", "values": [PASSWORD]},
        {"user_id": "bad-1", "key": "account_login", "values": {"platform_name": PLATFORM, "password": 12345678}},
    ],
)
def test_bad_requests_never_echo(client, vault_key, payload):
    r = client.post("/api/secrets", json=payload)
    assert r.status_code == 400
    assert PASSWORD not in r.text and "12345678" not in r.text


def test_malformed_body(client, vault_key):
    r = client.post("/api/secrets", content=b"{not json " + PASSWORD.encode(), headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert PASSWORD not in r.text


def test_no_plaintext_in_logs_db_memory_or_llm(client, vault_key, fake_llm, data_dir, caplog, monkeypatch):
    from app.gateway import llm_gateway

    monkeypatch.setattr(llm_gateway, "has_llm", lambda *a, **k: True)
    caplog.set_level(logging.DEBUG)
    uid = "leak-check"
    assert _save(client, uid, values={**LOGIN_VALUES, "cookie": COOKIE}).status_code == 200
    assert _save(client, uid, key="id_card", values={"id_number": ID_CARD}).status_code == 200
    client.get("/api/secrets", params={"user_id": uid})

    fake_llm.reply = "好的，我记下了。"
    r = client.post("/api/chat", json={"user_id": uid, "message": "我已经保存了网盘的账号，接下来做什么？"})
    assert r.status_code == 200
    system_prompt = fake_llm.calls[-1][0] if fake_llm.calls else ""
    assert "账号登录信息" in "\n".join(s for s, _ in fake_llm.calls)

    mem = client.get(f"/api/memory/{uid}", params={"limit": 100})
    traces = client.get(f"/api/traces/{r.json()['thread_id']}")
    blobs = [r.text, mem.text, traces.text, caplog.text, fake_llm.all_text(), system_prompt]
    blobs += [p.read_bytes().decode("utf-8", "ignore") for p in data_dir.glob("*.db*")]
    for secret in (PASSWORD, PHONE, ID_CARD, "abcdefgh12345678", "qqqqqqqqwwwwwww"):
        for blob in blobs:
            assert secret not in blob
    assert "已安全保存 账号登录信息" in mem.text


def test_chat_with_password_is_intercepted(client, vault_key, fake_llm, data_dir, caplog):
    caplog.set_level(logging.DEBUG)
    msg = f"帮我登录网盘，账号{PHONE} 密码：{PASSWORD}"
    r = client.post("/api/chat", json={"user_id": "typed-secret", "message": msg})
    assert r.status_code == 200
    data = r.json()["data"]
    assert fake_llm.calls == []
    assert data["ask"]["type"] == "secret" and data["ask"]["key"] == "account_login"
    assert set(data["privacy"]["redacted"]) == {"password", "phone"}
    assert PASSWORD not in r.text and PHONE not in r.text
    assert "[已隐藏]" in data["redacted_message"] and "[手机号已隐藏]" in data["redacted_message"]
    mem = client.get("/api/memory/typed-secret", params={"limit": 50}).text
    for blob in [mem, caplog.text] + [p.read_bytes().decode("utf-8", "ignore") for p in data_dir.glob("*.db*")]:
        assert PASSWORD not in blob and PHONE not in blob


def test_draft_request_with_password_is_intercepted(client, fake_llm, monkeypatch):
    from app import main

    uid = "draft-secret"

    def unexpected_graph_call(*args, **kwargs):
        pytest.fail("Credential-bearing draft request reached the graph")

    monkeypatch.setattr(main, "run_action", unexpected_graph_call)
    r = client.post(
        "/api/draft",
        json={"user_id": uid, "request": "帮我写一封邮件，告诉同事网盘 password: ExampleSecret123"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["privacy"]["redacted"] == ["password"]
    assert data["ask"]["type"] == "secret" and data["ask"]["key"] == "account_login"
    assert "ExampleSecret123" not in r.text
    mem = client.get(f"/api/memory/{uid}", params={"limit": 50}).text
    assert "ExampleSecret123" not in mem
    assert fake_llm.calls == []


def test_chat_with_cookie_and_id_card(client, vault_key, fake_llm):
    r = client.post("/api/chat", json={"user_id": "typed-2", "message": f"网盘的 cookie: {COOKIE}"})
    data = r.json()["data"]
    assert data["ask"]["key"] == "account_login"
    assert "abcdefgh12345678" not in r.text
    r = client.post("/api/chat", json={"user_id": "typed-2", "message": f"我身份证号是 {ID_CARD}"})
    data = r.json()["data"]
    assert data["ask"]["key"] == "id_card"
    assert ID_CARD not in r.text
    assert fake_llm.calls == []


def test_channel_inbound_is_redacted(vault_key, fake_llm):
    from app.channels.binding import bind_channel
    from app.data_access import system_scope
    with system_scope():
        bind_channel("feishu", "ou_x", "chan-user")
    from app.main import _handle_channel_inbound

    parsed = {
        "type": "inbound",
        "inbound": {
            "bound": True,
            "text": f"网盘密码是 {PASSWORD}",
            "user_id": "chan-user",
            "channel": "feishu",
            "external_user_id": "ou_x",
            "raw": {"text": PASSWORD},
        },
    }
    out = _handle_channel_inbound(parsed, "feishu")
    # PR #10: channels redirect private info to the web sandbox link instead of the in-page input.
    assert out["status"] == "privacy_redirect"
    assert PASSWORD not in json.dumps(out, ensure_ascii=False)
    assert "/sandbox" in json.dumps(out["outbound"], ensure_ascii=False)
    assert fake_llm.calls == []
