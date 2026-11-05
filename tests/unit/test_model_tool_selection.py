"""Model choices, offline fallback and unchanged execution authorization."""
import json
from unittest.mock import Mock

import pytest

from app.agent import router, tool_registry as registry, tool_selection as selection

DRAFT_TEXT = "帮我写一封请假邮件，周五请一天假"


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(selection.llm_gateway, "has_llm", lambda *a, **kw: True)
    model = Mock(return_value={"tool": "draft_text", "confidence": 0.95})
    monkeypatch.setattr(selection.llm_gateway, "chat_json", model)
    return model


def test_model_beats_keywords_and_receives_registry_schemas(model, monkeypatch):
    monkeypatch.setattr(router, "_classify_text", Mock(side_effect=AssertionError("heuristic used")))
    message = "把这段话改得客气一点再发给房东"
    result = router.classify_text_result(message)
    assert result.intent == "draft"
    assert result.rule_id == "model_tool:draft_text"
    system, user = model.call_args.args
    assert json.loads(user)["message"] == message
    for item in registry.list_tools():
        if item["name"] in selection.TOOL_ROUTES:
            assert json.dumps(item, ensure_ascii=False) in system
    assert '"requires_confirm": false' in system
    assert '"params_schema"' in system
    assert model.call_args.kwargs["fallback"] == {}
    assert model.call_args.kwargs["task"] == "tool_selection"


@pytest.mark.parametrize("name,intent", selection.TOOL_ROUTES.items())
def test_each_supported_tool_routes_to_existing_workflow(model, name, intent):
    model.return_value = {"tool": name, "confidence": 0.9}
    assert intent in router._ACTION_MAP
    assert router.route_intent(text="帮我处理一下这件事") == intent


def test_routes_cover_only_chat_workflows():
    assert set(selection.TOOL_ROUTES.values()) == {"chat", "draft"}
    assert selection.TOOL_ROUTES["draft_text"] == "draft"
    assert all(selection.TOOL_ROUTES[f"schedule.{op}"] == "chat"
               for op in ("create", "list", "pause", "resume", "update", "delete", "run_now"))
    # Infrastructure and memory-mutating tools are never model-selectable.
    assert not any(name.startswith(("memory_", "profile_", "proactive.")) and name != "memory_facts_search"
                   for name in selection.TOOL_ROUTES)


def test_confident_no_tool_does_not_use_keyword_fallback(model, monkeypatch):
    model.return_value = {"tool": None, "confidence": 0.9}
    monkeypatch.setattr(router, "_classify_text", Mock(side_effect=AssertionError("heuristic used")))
    assert router.route_intent(text="别写邮件了，陪我聊聊") == "chat"


@pytest.mark.parametrize("answer", [
    {}, [], {"tool": "draft_text"},
    {"tool": "draft_text", "confidence": 0.79},
    {"tool": "draft_text", "confidence": True},
    {"tool": "draft_text", "confidence": "0.99"},
    {"tool": "draft_text", "confidence": float("nan")},
    {"tool": "draft_text", "confidence": float("inf")},
    {"tool": "draft_text", "confidence": 1.01},
    {"tool": "invented", "confidence": 1},
    {"tool": ["draft_text"], "confidence": 1},
    {"tool": "memory_forget", "confidence": 1},
    {"tool": "proactive.deliver", "confidence": 1},
    {"tool": "draft_text", "confidence": 1, "arguments": {"user_id": "other"}},
    {"tool": "draft_text", "confidence": 1, "confirmed": True},
    {"tool": "draft_text", "confidence": 1, "_mode": "mock_fallback"},
    {"tool": "draft_text", "confidence": 1, "_parse": "fallback"},
])
def test_unreliable_live_model_does_not_use_keyword_fallback(model, answer):
    model.return_value = answer
    result = router.classify_text_result(DRAFT_TEXT)
    assert result.intent == "chat"
    assert result.rule_id == "chat_fallback"


def test_unavailable_model_and_timeout_fall_back(model, monkeypatch):
    monkeypatch.setattr(selection.llm_gateway, "has_llm", lambda *a, **kw: False)
    assert router.route_intent(text=DRAFT_TEXT) == "draft"
    model.assert_not_called()
    monkeypatch.setattr(selection.llm_gateway, "has_llm", lambda *a, **kw: True)
    model.side_effect = TimeoutError("private contents")
    assert router.classify_text_result(DRAFT_TEXT).rule_id == "chat_fallback"


@pytest.mark.parametrize("message", ["我想死", "帮我写封遗书，我不想活了"])
def test_safety_guards_precede_model(model, message):
    result = router.classify_text_result(message)
    assert result.rule_id == "crisis" and result.intent == "chat"
    model.assert_not_called()


@pytest.mark.parametrize("message", ["过了", "帮我弄一下"])
@pytest.mark.parametrize("tool,intent", [("draft_text", "draft"), (None, "chat")])
def test_vague_message_uses_model_choice(model, monkeypatch, message, tool, intent):
    model.return_value = {"tool": tool, "confidence": 0.95}
    monkeypatch.setattr(router, "_classify_text", Mock(side_effect=AssertionError("keyword bypass")))
    assert router.route_intent(text=message) == intent
    model.assert_called_once()
    assert "Use null for conversation" in model.call_args.args[0]
    assert "selection is not approval" in model.call_args.args[0]


def test_explicit_actions_and_resume_do_not_select_tools(model):
    assert router.route_intent({"action": "update_profile"}, text="whatever") == "update_profile"
    assert router.route_intent({"action": "draft"}, text="whatever") == "draft"
    assert router.route_intent({"_resume": {}}, text="whatever") == "confirm"
    model.assert_not_called()


def test_selection_is_cached_per_message(model):
    state = {}
    assert router.intent_result_for(state, "写个周报") == router.intent_result_for(state, "写个周报")
    assert model.call_count == 1
    router.intent_result_for(state, "再写个日报")
    assert model.call_count == 2


def test_model_selectable_tools_are_never_external():
    # External actions must stay behind confirm cards, never a model tool route.
    from app.agent import tool_selection
    registry.ensure_defaults()
    risk = {tool["name"]: tool["risk_level"] for tool in registry.list_tools()}
    assert tool_selection.TOOL_ROUTES
    assert all(risk.get(name) != "external" for name in tool_selection.TOOL_ROUTES)


def test_web_model_choice_reaches_draft_workflow_without_keywords(client, model, monkeypatch):
    from app import main
    model.return_value = {"tool": "draft_text", "confidence": 0.95}
    run = Mock(return_value={"thread_id": "selection-web", "state": {}, "awaiting_confirm": False})
    monkeypatch.setattr(main, "run_action", run)
    message = "帮我整理一下这段话，发给同事"
    assert not router.looks_like_draft_request(message)
    response = client.post("/api/chat", json={"user_id": "selection-web", "message": message})
    assert response.status_code == 200
    payload = run.call_args.args[0]
    assert payload["action"] == "draft"
    assert payload["draft_request"] == message
    assert payload["message"] == message
    assert model.call_count == 1


def test_web_confident_no_tool_overrides_draft_keywords(client, model, fake_llm, monkeypatch):
    from app.agent.subgraphs import chat
    # This routing test must not leave summary workers for later span tests.
    monkeypatch.setattr(chat, "maybe_summarize_turn", Mock())
    model.return_value = {"tool": None, "confidence": 0.95}
    message = "别帮我写邮件了，今天只想聊聊生活里的开心事。"
    assert router.looks_like_draft_request(message)
    response = client.post("/api/chat", json={"user_id": "selection-chat", "message": message})
    assert response.status_code == 200
    assert response.json()["status"] == "chat"
    assert response.json()["data"]["intent"] == "chat"
    assert model.call_count == 1
