"""Sports-buddy fork: six registered tools, storage, prompt data and the API."""

import json

import pytest

from app.agent import tool_registry, tool_selection
from app.data_access import user_scope
from app.gateway import llm_gateway
from app.sports import chat as sport_chat
from app.sports import store

SPORT_TOOLS = {
    "sport.profile_get": "read",
    "sport.profile_update": "self_write",
    "sport.log_workout": "self_write",
    "sport.history": "read",
    "sport.plan_get": "read",
    "sport.plan_save": "self_write",
}


def test_sport_tools_registered_and_routed():
    tools = {item["name"]: item for item in tool_registry.list_tools()}
    for name, risk in SPORT_TOOLS.items():
        assert name in tools, name
        assert tools[name]["risk_level"] == risk
        assert tools[name]["requires_confirm"] is False
        assert tool_selection.TOOL_ROUTES[name] == "chat"
    # Nothing here is an outbound tool: no confirmation credential required.
    assert all(tools[name]["risk_level"] != "external" for name in SPORT_TOOLS)


def test_profile_workout_and_stats(uid):
    declared = tool_registry.invoke_tool("sport.profile_update", {
        "goal": "半马 2 小时", "level": "初级", "days_per_week": 3,
        "session_minutes": 40, "injuries": "右膝外侧偶尔不适",
        "equipment": ["跑鞋", "弹力带"],
    })
    assert declared["ok"] is True
    assert declared["profile"]["sport_goal"] == "半马 2 小时"
    assert declared["profile"]["sport_equipment"] == ["跑鞋", "弹力带"]

    first = tool_registry.invoke_tool("sport.log_workout", {
        "sport": "跑步", "duration_min": 32, "distance_km": 5.0, "intensity": "中等",
    })
    assert first["session"]["summary"] == "跑步 32 分钟 · 5 公里 · 中等"
    tool_registry.invoke_tool("sport.log_workout", {"sport": "力量训练", "duration_min": 25})

    stats = tool_registry.invoke_tool("sport.history", {"days": 7})["stats"]
    assert stats["sessions"] == 2
    assert stats["minutes"] == 57
    assert stats["distance_km"] == 5.0
    assert stats["by_sport"] == {"力量训练": 1, "跑步": 1}

    # Another user sees nothing: identity comes from the trusted scope.
    with user_scope("u-someone-else"):
        assert tool_registry.invoke_tool("sport.history", {"days": 7})["stats"]["sessions"] == 0
        assert tool_registry.invoke_tool("sport.profile_get", {})["profile"] == {}


def test_workout_validation_and_plan_roundtrip(uid):
    with pytest.raises(ValueError):
        store.log_workout(uid, sport="跑步", duration_min=0)
    with pytest.raises(ValueError):
        store.save_plan(uid, goal="")

    saved = tool_registry.invoke_tool("sport.plan_save", {
        "goal": "10 公里跑进 60 分钟", "weeks": 6, "days_per_week": 3, "session_minutes": 45,
        "items": [{"day": "周一", "focus": "轻松跑", "duration_min": 30, "intensity": "轻松"}],
    })
    assert saved["plan"]["weeks"] == 6
    loaded = tool_registry.invoke_tool("sport.plan_get", {})
    assert loaded["plan"]["goal"] == "10 公里跑进 60 分钟"
    assert "周一：轻松跑" in loaded["text"]


def test_plan_is_written_to_profile_and_shown_in_memory_card(uid):
    store.update_profile(uid, {"sport_goal": "减脂", "sport_days_per_week": 3})
    store.save_plan(uid, goal="减脂", weeks=4, days_per_week=3,
                    items=[{"day": "周二", "focus": "力量", "duration_min": 30}])

    from app.agent.memory.card import card_view
    from app.agent.memory.profile import format_for_prompt

    labels = {fact["label"]: fact["value"] for fact in card_view(uid)["facts"]}
    assert labels["运动目标"] == "减脂"
    assert labels["每周训练天数"] == "3"
    prompt = format_for_prompt(uid)
    assert "运动目标：减脂" in prompt and "每周训练：3 天" in prompt


def test_prompt_block_is_bounded_untrusted_data(uid):
    store.update_profile(uid, {"sport_goal": "恢复跑步"})
    store.log_workout(uid, sport="跑步", duration_min=20)
    block = store.prompt_block(uid)
    assert block.startswith("【运动数据】")
    assert "<memory>" in block and "</memory>" in block
    payload = json.loads(block.split("<memory>", 1)[1].split("</memory>", 1)[0])
    assert payload["sport_profile"]["sport_goal"] == "恢复跑步"
    assert payload["last_7_days"]["sessions"] == 1
    # No data for an unknown user: the prompt stays clean.
    with user_scope("u-never-seen"):
        assert store.prompt_block("u-never-seen") == ""


def test_chat_adapter_extracts_arguments_and_replies(uid, monkeypatch):
    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **k: {
        "args": {"sport": "游泳", "duration_min": 45, "distance_km": 1.5},
    })
    out = sport_chat.respond(uid, "sport.log_workout", "今天游了 1500 米，45 分钟")
    text = out["messages"][0]["content"]
    assert "记下了" in text and "游泳" in text
    assert store.stats(uid, days=7)["sessions"] == 1


def test_chat_adapter_asks_instead_of_guessing(uid, monkeypatch):
    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **k: {"question": "这次练了多久？"})
    out = sport_chat.respond(uid, "sport.log_workout", "我练完了")
    assert out["messages"][0]["content"] == "这次练了多久？"
    assert out["_pending_sport"]["turns"][0] == "这次练了多久？"
    assert store.stats(uid, days=7)["sessions"] == 0


def test_summary_api_is_owner_scoped(client, auth, uid):
    store.log_workout(uid, sport="骑行", duration_min=60, distance_km=20)
    headers = auth(uid)
    ok = client.get("/api/sports/summary?days=7", headers=headers)
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["stats"]["sessions"] == 1
    assert data["stats"]["distance_km"] == 20.0

    other = client.get(f"/api/sports/summary?user_id=u-another-user", headers=headers)
    assert other.status_code == 404


def test_chat_request_routes_into_the_sport_tool(client, auth, uid, monkeypatch):
    """The real /api/chat wiring: model picks the tool, adapter fills args, tool writes."""
    def fake_json(system, user, **kwargs):
        if "Select one tool" in system:
            return {"tool": "sport.log_workout", "confidence": 0.93}
        if "运动工具提取参数" in system:
            return {"args": {"sport": "跑步", "duration_min": 30, "distance_km": 4.2}}
        return {}

    monkeypatch.setattr(llm_gateway, "chat_json", fake_json)
    monkeypatch.setattr(llm_gateway, "has_llm", lambda *args, **kwargs: True)
    response = client.post("/api/chat", headers=auth(uid), json={
        "user_id": uid, "message": "今天跑了 4.2 公里，30 分钟",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "chat"
    assert "记下了" in body["data"]["messages"][0]["content"]
    assert store.stats(uid, days=7)["sessions"] == 1
