"""Companion: energy math, avatar, meals, dormancy, social style and the API."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agent import tool_registry, tool_selection
from app.companion import chat as companion_chat
from app.companion import energy, meals, pose, store, vision
from app.data_access import user_scope
from app.gateway import llm_gateway
from app.sports import store as sport_store

COMPANION_TOOLS = {
    "companion.set_body": "self_write",
    "companion.status": "read",
    "companion.log_meal": "self_write",
    "companion.meal_suggest": "read",
    "companion.recap": "read",
    "companion.set_style": "self_write",
    "companion.revive": "self_write",
}


def test_companion_tools_registered_and_routed():
    tools = {item["name"]: item for item in tool_registry.list_tools()}
    for name, risk in COMPANION_TOOLS.items():
        assert name in tools, name
        assert tools[name]["risk_level"] == risk
        assert tools[name]["requires_confirm"] is False
        assert tool_selection.TOOL_ROUTES[name] == "chat"


def test_energy_math_is_transparent():
    day = energy.daily(height_cm=170, weight_kg=70, age=30, sex="male",
                       activity_level="moderate", intake_kcal=600,
                       sessions=[{"payload": {"sport": "跑步", "duration_min": 40, "intensity": "中等"}}])
    assert 1550 < day.bmr < 1700
    assert day.base_burn == pytest.approx(day.bmr * 1.2, abs=1)
    assert day.exercise_burn == pytest.approx(10.0 * 70 * (40 / 60), abs=0.1)
    assert day.net == pytest.approx(600 - day.base_burn - day.exercise_burn, abs=1)
    assert day.direction == "减重" and day.weekly_kg < 0
    # No intake yet is not a deficit.
    assert energy.daily(height_cm=170, weight_kg=70, intake_kcal=0).net < 0


def test_setup_status_and_avatar(uid):
    assert store.status(uid)["needs_setup"] is True
    saved = tool_registry.invoke_tool("companion.set_body", {
        "height_cm": 170, "weight_kg": 70, "body_fat_pct": 22, "age": 30,
        "sex": "male", "activity_level": "moderate", "goal": "cut",
    })
    assert saved["ok"] is True
    state = tool_registry.invoke_tool("companion.status", {})["status"]
    assert state["needs_setup"] is False
    assert state["avatar"]["roundness"] > 0  # BMI 24.2 → rounder than the 22 reference
    assert state["energy"]["net"] is None and state["has_intake"] is False
    assert state["hunger_days_left"] == store.dormancy_days()

    store.set_body(uid, height_cm=170, weight_kg=58, body_fat_pct=18)
    lighter = tool_registry.invoke_tool("companion.status", {})["status"]
    assert lighter["avatar"]["roundness"] < state["avatar"]["roundness"]


def test_meal_and_workout_change_the_balance(uid):
    store.set_body(uid, height_cm=170, weight_kg=70, age=30, sex="male",
                   activity_level="moderate", goal="cut")
    meal = tool_registry.invoke_tool("companion.log_meal", {
        "name": "牛肉面", "kcal": 600, "protein_g": 28, "source": "estimate",
    })
    assert meal["meal"]["payload"]["kcal"] == 600.0
    sport_store.log_workout(uid, sport="跑步", duration_min=40, distance_km=6, intensity="中等")
    state = tool_registry.invoke_tool("companion.status", {})["status"]
    assert state["energy"]["intake"] == 600
    assert state["energy"]["exercise_burn"] == pytest.approx(467, abs=2)
    assert state["energy"]["net"] is not None and state["energy"]["direction"] == "减重"
    assert state["streak_days"] == 1


def test_dormancy_farewell_and_revive(uid):
    store.set_body(uid, height_cm=170, weight_kg=70)
    tool_registry.invoke_tool("companion.log_meal", {"name": "燕麦", "kcal": 300})
    later = datetime.now(timezone.utc) + timedelta(days=store.dormancy_days() + 1)
    dead = store.status(uid, now=later)
    assert dead["dead"] is True
    assert "陪你走了" in dead["farewell"] and "不怪你" in dead["farewell"]
    # Reviving starts a new generation and clears dormancy.
    alive = tool_registry.invoke_tool("companion.revive", {})["status"]
    assert alive["generation"] == 2 and alive["dead"] is False
    assert alive["hunger_days_left"] == store.dormancy_days()


def test_recap_reports_volume_and_intake(uid):
    store.set_body(uid, height_cm=170, weight_kg=70)
    sport_store.log_workout(uid, sport="骑行", duration_min=60, distance_km=20)
    tool_registry.invoke_tool("companion.log_meal", {"name": "鸡胸沙拉", "kcal": 380})
    rec = tool_registry.invoke_tool("companion.recap", {"days": 7})["recap"]
    assert rec["sessions"] == 1 and rec["minutes"] == 60
    assert rec["meals"] == 1 and rec["avg_intake"] == 380
    assert "训练 1 次" in rec["summary"]


def test_social_style_from_counters_and_manual_override(uid):
    store.set_body(uid, height_cm=170, weight_kg=70)
    for text in ("今天练了什么呀？", "我也想试试！", "组间休息聊两句😄"):
        store.observe_message(uid, text)
    social = store.social_profile(uid)
    assert social["samples"] == 3 and social["label"] in {"偏内向", "中间型", "偏外向"}
    manual = tool_registry.invoke_tool("companion.set_style", {"social_label": "偏内向"})["social"]
    assert manual["label"] == "偏内向" and manual["source"] == "manual"


def test_meal_options_respect_budget_and_time():
    options = meals.suggest(remaining_kcal=500, goal="cut", time_of_day="午餐",
                            protein_target_g=100, protein_so_far_g=40, limit=3)
    assert len(options) == 3
    assert all(item["kcal"] <= 620 for item in options)
    assert all(item["reason"] for item in options)
    small = meals.suggest(remaining_kcal=120, goal="maintain", time_of_day="加餐", limit=3)
    assert min(item["kcal"] for item in small) <= 380


def test_vision_fails_closed_without_provider(monkeypatch):
    for key in ("VISION_MODEL", "LLM_FAST_API_KEY", "OPENAI_API_KEY", "KIMI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert vision.has_vision() is False
    with pytest.raises(Exception) as info:
        vision.analyze_food(b"\xff\xd8\xff\xe0" + b"0" * 16)
    assert getattr(info.value, "code", "") == "vision_not_configured"
    with pytest.raises(Exception) as empty:
        vision.analyze_food(b"")
    assert getattr(empty.value, "code", "") == "empty_image"


def test_pose_is_honest_about_not_being_connected():
    status = pose.pose_status()
    assert status["configured"] is False and status["status"] == "not_connected"
    assert pose.analyze_pose(b"frame")["status"] == "not_connected"


def test_chat_adapter_logs_meal_and_suggests_options(uid, monkeypatch):
    store.set_body(uid, height_cm=170, weight_kg=70, goal="cut")
    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **k: {
        "args": {"name": "牛肉面", "kcal": 600, "protein_g": 28, "source": "estimate"},
    })
    out = companion_chat.respond(uid, "companion.log_meal", "我吃了一碗牛肉面")
    assert "记下了" in out["messages"][0]["content"]
    assert store.status(uid)["energy"]["intake"] == 600

    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **k: {"args": {}})
    out = companion_chat.respond(uid, "companion.meal_suggest", "不知道吃什么")
    card = out["ask"]
    assert card["type"] == "single" and len(card["options"]) == 4
    assert card["options"][-1] == "都不想吃，换一批"


def test_chat_adapter_asks_before_guessing(uid, monkeypatch):
    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **k: {"question": "你多重、多高？"})
    out = companion_chat.respond(uid, "companion.set_body", "给我建个搭子")
    assert out["messages"][0]["content"] == "你多重、多高？"
    assert out["_pending_companion"]["turns"][0] == "你多重、多高？"
    assert store.status(uid)["needs_setup"] is True


def test_companion_api_setup_meal_and_revive(client, auth, uid):
    headers = auth(uid)
    empty = client.get("/api/companion", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["data"]["needs_setup"] is True

    created = client.post("/api/companion/profile", headers=headers, json={
        "user_id": uid, "height_cm": 170, "weight_kg": 70, "body_fat_pct": 20,
        "age": 30, "sex": "male", "activity_level": "moderate", "goal": "cut",
        "name": "小壮",
    })
    assert created.status_code == 200
    data = created.json()["data"]["status"]
    assert data["name"] == "小壮" and data["avatar"]["roundness"] > 0

    meal = client.post("/api/companion/meal", headers=headers, json={
        "user_id": uid, "name": "鸡胸沙拉", "kcal": 380,
    })
    assert meal.status_code == 200
    assert meal.json()["data"]["status"]["energy"]["intake"] == 380

    revived = client.post("/api/companion/revive", headers=headers, json={"user_id": uid})
    assert revived.status_code == 200
    assert revived.json()["data"]["status"]["generation"] == 2

    other = client.get("/api/companion?user_id=u-another-user", headers=headers)
    assert other.status_code == 404
