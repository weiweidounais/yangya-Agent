"""HTTP + channel surface for the loop: reject reasons, redraft, safety intact."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.subgraphs.chat import HOTLINE_MARK
from app.main import _handle_channel_inbound, app
from tests.conftest import DRAFT_REQUEST


@pytest.fixture
def client():
    return TestClient(app)


def _draft(client, uid):
    r = client.post("/api/chat", json={"user_id": uid, "message": DRAFT_REQUEST})
    assert r.status_code == 200
    body = r.json()
    assert body["awaiting_confirm"] is True
    assert body["data"]["task_card"]["type"] == "draft_confirm"
    return body["thread_id"]


def test_confirm_endpoint_reject_reason_redrafts(client, uid):
    tid = _draft(client, uid)
    r = client.post(
        "/api/confirm",
        json={"user_id": uid, "thread_id": tid, "confirmed": False, "reason": "太正式了"},
    )
    body = r.json()
    assert body["status"] == "awaiting_confirm"
    assert body["data"]["reject_reason"] == "太正式了"
    assert {"source": "user_reject", "reason": "太正式了"} in body["data"]["loop"]["feedback"]
    assert body["data"]["draft"]["body"]


def test_chat_typed_reject_with_reason(client, uid):
    tid = _draft(client, uid)
    r = client.post("/api/chat", json={"user_id": uid, "thread_id": tid, "message": "拒绝：太浮夸了"})
    body = r.json()
    assert body["status"] == "awaiting_confirm"
    assert body["data"]["needs_confirm"] is True
    assert "太浮夸了" in body["data"]["messages"][-1]["content"]


def test_chat_typed_confirm_does_not_bypass_card(client, uid):
    tid = _draft(client, uid)
    client.post("/api/chat", json={"user_id": uid, "thread_id": tid, "message": "确认"})
    r = client.get(f"/api/confirm/{tid}", params={"user_id": uid})
    assert r.json()["data"]["decision"] is None


def test_confirm_with_edits_keeps_user_text(client, uid):
    tid = _draft(client, uid)
    r = client.post(
        "/api/confirm",
        json={"user_id": uid, "thread_id": tid, "confirmed": True, "edits": {"body": "用户自己改的正文"}},
    )
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["data"]["draft"]["body"] == "用户自己改的正文"
    assert "已确认" in body["data"]["messages"][-1]["content"]


def test_channel_reject_with_reason(uid, client):
    from app.channels.binding import bind_channel
    bind_channel("feishu", "ou_x", uid)
    tid = _draft(client, uid)
    parsed = {
        "inbound": {
            "bound": True,
            "text": "拒绝：别提金额",
            "user_id": uid,
            "channel": "feishu",
            "external_user_id": "ou_x",
            "thread_id": tid,
        }
    }
    out = _handle_channel_inbound(parsed, "feishu")
    assert out["status"] == "awaiting_confirm"
    assert out["result"]["data"]["reject_reason"] == "别提金额"


def test_chat_crisis_still_handled(client, uid):
    r = client.post("/api/chat", json={"user_id": uid, "message": "我真的撑不下去了，想结束一切"})
    msg = r.json()["data"]["messages"][-1]["content"]
    assert HOTLINE_MARK in msg


def test_capabilities_advertise_loop(client):
    loop = client.get("/api/capabilities").json()
    text = str(loop)
    assert "observe_plan_tool_validate" in text
