"""Hybrid dispatch tested through endpoint handlers and authenticated HTTP."""
import pytest

from app.agent import intent_scoring
from tests.conftest import DRAFT_REQUEST

# Scorer winners that must never dispatch a task from message text.
UNSUPPORTED = ("confirm", "draft", "update_profile", "schedule")


@pytest.fixture(autouse=True)
def offline_hybrid(monkeypatch):
    import socket
    from app.gateway import llm_gateway
    monkeypatch.setenv("INTENT_SCORING_MODE", "hybrid")
    monkeypatch.setattr(llm_gateway, "has_llm", lambda *args, **kwargs: False)
    monkeypatch.setattr(llm_gateway, "chat_text", lambda *args, **kwargs: "我在这里陪你。")
    def no_network(*args, **kwargs):
        pytest.fail("endpoint regressions must not use the network")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)


@pytest.fixture
def embedding(monkeypatch):
    def install(label, confidence=.99):
        scores = {name: .1 for name in intent_scoring.LABELS}
        scores[label] = .9
        monkeypatch.setattr(intent_scoring, "score_text", lambda _: (
            scores, dict(confidence=confidence, margin=.8, model_version="mock", exemplar_version="test")))
    return install


def handler_post(path, payload):
    from app import main
    from app.models import ChatRequest, DraftRequest
    endpoint, model = {"/api/chat": (main.chat, ChatRequest),
                       "/api/draft": (main.draft, DraftRequest)}[path]
    assert any(getattr(route, "path", None) == path and route.endpoint is endpoint for route in main.app.routes)
    return endpoint(model(**payload)).model_dump()


def http_post(client):
    def post(path, payload):
        response = client.post(path, json=payload)
        assert response.status_code == 200, response.text
        return response.json()
    return post


def assert_chat_falls_back(post, uid, label, embedding):
    from app import graph
    embedding(label)
    body = post("/api/chat", {"user_id": uid, "message": "帮我处理一下这件事"})
    assert body["status"] == "chat"
    assert body["data"]["intent"] == "chat"
    state = graph.get_thread_snapshot(body["thread_id"])["values"]
    assert state["intent_result"]["rule_id"] == "chat_fallback"
    assert state["intent_result"]["decision"] == "fallback"
    assert not state.get("draft")


@pytest.mark.parametrize("label", UNSUPPORTED)
def test_chat_handler_unsupported_hybrid_route(uid, label, embedding):
    assert_chat_falls_back(handler_post, uid, label, embedding)


@pytest.mark.parametrize("label", UNSUPPORTED)
def test_http_chat_unsupported_hybrid_route(client, uid, label, embedding):
    assert_chat_falls_back(http_post(client), uid, label, embedding)


def assert_crisis_then_draft(post, uid, embedding):
    from app import graph
    embedding("chat", .2)
    first = post("/api/chat", {"user_id": uid, "message": "不想活了"})
    tid = first["thread_id"]
    assert first["status"] == "chat"
    assert graph.get_thread_snapshot(tid)["values"]["crisis_detected"] is True
    second = post("/api/draft", {"user_id": uid, "thread_id": tid, "request": DRAFT_REQUEST})
    assert second["status"] == "awaiting_confirm"
    assert second["data"]["task_card"]["type"] == "draft_confirm"
    assert second["data"]["draft"]["body"]
    state = graph.get_thread_snapshot(tid)["values"]
    assert state["crisis_detected"] is False
    assert state["message"] == state["draft_request"] == DRAFT_REQUEST


def test_draft_handler_after_crisis_on_same_thread(uid, embedding):
    assert_crisis_then_draft(handler_post, uid, embedding)


def test_http_draft_after_crisis_on_same_thread(client, uid, embedding):
    assert_crisis_then_draft(http_post(client), uid, embedding)


@pytest.mark.parametrize("limit", ["steps", "cost"])
def test_embedding_only_crisis_bypasses_exhausted_budget(uid, embedding, monkeypatch, limit):
    from app import graph
    from app.agent import cost_ledger
    from app.agent.crisis_rules import has_crisis_signal
    from app.agent.subgraphs.chat import HOTLINE_MARK

    message = "明天不会再见了"
    assert not has_crisis_signal(message)
    embedding("crisis")
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    limits = {}
    if limit == "cost":
        monkeypatch.setenv("AGENT_COST_CAP_FREE_DAILY", "1")
        cost_ledger.record_spend(uid, 10.0)
        assert not cost_ledger.check(uid).ok
    else:
        limits = {"loop_step": 1, "loop_budget": {"max_steps": 1}}

    result = graph.run_action({"user_id": uid, "action": "chat", "message": message, **limits})
    state = result["state"]
    assert state["intent_result"]["rule_id"] == "crisis"
    assert state["intent_result"]["safety_override"] is True
    assert HOTLINE_MARK in state["messages"][-1]["content"]
    assert not state.get("error")
    assert not state.get("pending_continue")
    assert not result["loop"].get("continue_card")

    embedding("chat", .2)
    normal = graph.run_action({"user_id": uid, "action": "chat", "message": "你好", **limits},
                              thread_id=result["thread_id"])
    assert normal["state"]["error"].startswith("loop_budget:")
    assert normal["loop"]["continue_card"]


def test_chat_turn_after_draft_keeps_draft_request(uid, embedding):
    from app import graph

    embedding("chat", .2)
    first = handler_post("/api/draft", {"user_id": uid, "request": DRAFT_REQUEST})
    assert first["status"] == "awaiting_confirm"
    tid = first["thread_id"]
    second = handler_post("/api/chat", {"user_id": uid, "thread_id": tid, "message": "你好"})
    assert second["status"] == "chat"
    state = graph.get_thread_snapshot(tid)["values"]
    assert state["draft_request"] == DRAFT_REQUEST
    assert state["crisis_detected"] is False


@pytest.mark.parametrize("action, message", [("draft", DRAFT_REQUEST), ("chat", "")])
def test_retained_draft_request_is_not_a_current_turn_crisis(uid, embedding, action, message):
    from app import graph
    from app.agent.subgraphs.chat import HOTLINE_MARK

    embedding("chat", .2)
    document = "不想活了"
    first = graph.run_action({"user_id": uid, "action": "draft", "message": document, "draft_request": document})
    assert first["state"]["crisis_detected"] is True
    assert HOTLINE_MARK in first["state"]["messages"][-1]["content"]
    payload = {"user_id": uid, "action": action, "message": message}
    if action == "draft":
        payload["draft_request"] = message
    second = graph.run_action(payload, thread_id=first["thread_id"])
    # A retained request stays in the checkpoint but is never a current-turn input.
    assert second["state"]["draft_request"] == (DRAFT_REQUEST if action == "draft" else document)
    assert second["state"]["crisis_detected"] is False
    assert HOTLINE_MARK not in second["state"]["messages"][-1]["content"]
    if action == "draft":
        assert second["awaiting_confirm"] and second["state"]["draft"]["body"]
