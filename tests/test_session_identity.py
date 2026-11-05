"""Identity comes from the server session, never from a client user_id."""

from __future__ import annotations

import uuid

import pytest

from app.auth import mint_user_token
from app.main import app
from tests.conftest import DRAFT_REQUEST, RawTestClient


@pytest.fixture
def raw():
    return RawTestClient(app)


def H(uid):
    return {"X-User-Token": mint_user_token(uid)}


@pytest.fixture
def ab():
    return f"qa-a-{uuid.uuid4().hex[:8]}", f"qa-b-{uuid.uuid4().hex[:8]}"


def _thread(raw, uid):
    r = raw.post("/api/chat", json={"message": DRAFT_REQUEST}, headers=H(uid))
    assert r.status_code == 200 and r.json()["awaiting_confirm"] is True
    return r.json()["thread_id"]


def test_session_endpoint_mints_server_chosen_user(raw):
    r = raw.post("/api/session")
    assert r.status_code == 200
    uid = r.json()["data"]["user_id"]
    assert uid.startswith("u_") and "ja_session" in r.cookies
    # Cookie alone authenticates; a client cannot choose its id
    r2 = raw.post("/api/session", json={"user_id": "victim"})
    assert r2.json()["data"]["user_id"] == uid
    assert raw.get("/api/secrets").status_code == 200


def test_no_session_is_401(raw):
    for method, path in [
        ("get", "/api/secrets?user_id=x"),
        ("get", "/api/profile/x"),
        ("get", "/api/memory/x"),
        ("get", "/api/memory/x/card"),
        ("get", "/api/schedules"),
    ]:
        assert getattr(raw, method)(path).status_code == 401, path
    assert raw.post("/api/chat", json={"user_id": "x", "message": "hi"}).status_code == 401
    assert raw.post("/api/draft", json={"user_id": "x", "request": "写封信"}).status_code == 401


def test_fail_closed_without_auth_secret(raw, monkeypatch):
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    assert raw.get("/api/secrets").status_code == 503
    assert raw.post("/api/session").status_code == 503


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/profile/{a}"),
        ("get", "/api/memory/{a}"),
        ("get", "/api/memory/{a}/card"),
        ("post", "/api/memory/{a}/forget"),
        ("get", "/api/secrets?user_id={a}"),
        ("delete", "/api/secrets/phone?user_id={a}"),
    ],
)
def test_cross_account_path_and_query_is_404(raw, ab, method, path):
    a, b = ab
    r = getattr(raw, method)(path.format(a=a), headers=H(b))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_cross_account_body_is_404(raw, ab, vault_key):
    a, b = ab
    r = raw.post(
        "/api/secrets",
        json={"user_id": a, "key": "phone", "values": {"phone": "13900000000"}},
        headers=H(b),
    )
    assert r.status_code == 404
    r = raw.post("/api/profile", json={"user_id": a, "profile": {"city": "上海"}}, headers=H(b))
    assert r.status_code == 404
    r = raw.post("/api/chat", json={"user_id": a, "message": "hi"}, headers=H(b))
    assert r.status_code == 404
    r = raw.post("/api/draft", json={"user_id": a, "request": DRAFT_REQUEST}, headers=H(b))
    assert r.status_code == 404


def test_omitted_user_id_uses_session_not_demo(raw, ab, vault_key):
    a, b = ab
    r = raw.post("/api/secrets", json={"key": "phone", "values": {"phone": "13911112222"}}, headers=H(a))
    assert r.status_code == 200
    assert [i["key"] for i in raw.get("/api/secrets", headers=H(a)).json()["data"]["items"]] == ["phone"]
    assert raw.get("/api/secrets", headers=H(b)).json()["data"]["items"] == []
    # B cannot delete A's secret: it is scoped to B's own (empty) vault
    assert raw.delete("/api/secrets/phone", headers=H(b)).status_code == 404
    assert raw.delete("/api/secrets/phone", headers=H(a)).status_code == 200


def test_foreign_thread_is_404_on_chat_and_confirm(raw, ab):
    a, b = ab
    tid = _thread(raw, a)
    r = raw.post("/api/chat", json={"thread_id": tid, "message": "确认"}, headers=H(b))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "thread_not_found"
    assert raw.get(f"/api/confirm/{tid}", headers=H(b)).status_code == 404
    assert raw.post(f"/api/confirm/{tid}/token", headers=H(b)).status_code == 404
    r = raw.post("/api/confirm", json={"thread_id": tid, "confirmed": True}, headers=H(b))
    assert r.status_code == 404


def test_confirm_token_is_one_time_and_owner_bound(raw, ab):
    a, b = ab
    tid = _thread(raw, a)
    # no token → refused
    r = raw.post("/api/confirm", json={"thread_id": tid, "confirmed": True}, headers=H(a))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "confirm_token_required"
    tok = raw.post(f"/api/confirm/{tid}/token", headers=H(a)).json()["data"]["confirm_token"]
    # B's own thread cannot spend A's token
    tid_b = _thread(raw, b)
    r = raw.post("/api/confirm", json={"thread_id": tid_b, "confirmed": True, "confirm_token": tok}, headers=H(b))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "confirm_token_invalid"
    body = {"thread_id": tid, "confirmed": True, "confirm_token": tok}
    r1 = raw.post("/api/confirm", json=body, headers=H(a))
    assert r1.status_code == 200 and r1.json()["status"] == "confirmed"
    r2 = raw.post("/api/confirm", json=body, headers=H(a))
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "confirm_token_used"
    # A later, deliberate override still works with a fresh token (product lock)
    tok2 = raw.post(f"/api/confirm/{tid}/token", headers=H(a)).json()["data"]["confirm_token"]
    r3 = raw.post("/api/confirm", json={"thread_id": tid, "confirmed": False, "confirm_token": tok2}, headers=H(a))
    assert r3.status_code == 200


def test_memory_card_is_session_scoped(raw, ab):
    a, b = ab
    raw.post("/api/profile", json={"profile": {"facts": ["合成事实甲"]}}, headers=H(a))
    card_b = raw.get(f"/api/memory/{b}/card", headers=H(b)).json()["data"]
    assert "合成事实甲" not in str(card_b)
    assert raw.get(f"/api/memory/{a}/card", headers=H(b)).status_code == 404


def test_proactive_dismiss_is_owner_checked(raw, ab):
    from app.agent.proactive import store

    a, b = ab
    store._ensure()
    from app.agent.proactive.store import ProactiveItem
    from app.db import SessionLocal

    with SessionLocal() as s:
        it = ProactiveItem(user_id=a, kind="reminder", title="t", body="b", status="pending")
        s.add(it)
        s.commit()
        iid = it.id
    assert raw.post(f"/api/proactive/items/{iid}/dismiss", headers=H(b)).status_code == 404
    assert raw.post(f"/api/proactive/items/{iid}/dismiss", headers=H(a)).status_code == 200


@pytest.fixture(autouse=True)
def _trusted_test_setup():
    from app.data_access import system_scope
    with system_scope():
        yield
