"""Weekly report, photo analysis and rest timer (0.11 additions)."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.agent.proactive import store as proactive_store
from app.companion import chat as companion_chat
from app.companion import report, store, timer, vision
from app.data_access import user_scope
from app.gateway import llm_gateway

TZ = ZoneInfo("Asia/Shanghai")


def test_rest_timer_clamps_and_tips():
    card = timer.rest_card(90, exercise="深蹲", set_index=2, sets=4)
    assert card["seconds"] == 90 and card["label"] == "第 2 / 4 组"
    assert "膝盖" in card["tip"]
    assert timer.rest_card(3)["seconds"] == timer.MIN_SECONDS
    assert timer.rest_card(9999)["seconds"] == timer.MAX_SECONDS
    assert "休息 90 秒" in timer.rest_text(timer.rest_card(90, exercise="跑步"))


def test_weekly_report_contains_numbers_and_suggestion(uid):
    store.set_body(uid, height_cm=170, weight_kg=70, activity_level="moderate", goal="cut")
    built = report.build_report(uid, days=7)
    assert built["title"].endswith("本周周报")
    assert "训练" in built["body"] and "饮食" in built["body"]
    assert built["period"].endswith(tuple("0123456789")) and "W" in built["period"]
    assert "下周" in built["body"] or "下次" in built["body"]


def test_weekly_report_is_idempotent_and_in_app(monkeypatch, uid):
    store.set_body(uid, height_cm=170, weight_kg=70)
    sunday = datetime.now(TZ)
    sunday = (sunday + timedelta(days=(6 - sunday.weekday()) % 7)).replace(hour=20, minute=5, second=0, microsecond=0)
    moment = sunday.astimezone(timezone.utc)
    monkeypatch.setenv("COMPANION_WEEKLY_ENABLED", "1")
    monkeypatch.setenv("COMPANION_WEEKLY_WEEKDAY", "6")
    monkeypatch.setenv("COMPANION_WEEKLY_HOUR", "20")

    first = report.run_weekly_reports(now=moment)
    assert first["sent"] >= 1
    items = [i for i in proactive_store.list_pending(uid, status=None, limit=50)
             if i["kind"] == "companion_weekly"]
    assert len(items) == 1 and "本周周报" in items[0]["title"]

    second = report.run_weekly_reports(now=moment)
    assert second["sent"] == 0
    items = [i for i in proactive_store.list_pending(uid, status=None, limit=50)
             if i["kind"] == "companion_weekly"]
    assert len(items) == 1


def test_weekly_report_only_runs_in_window(monkeypatch, uid):
    store.set_body(uid, height_cm=170, weight_kg=70)
    monkeypatch.setenv("COMPANION_WEEKLY_WEEKDAY", "6")
    monkeypatch.setenv("COMPANION_WEEKLY_HOUR", "20")
    monday = datetime.now(TZ)
    monday = (monday - timedelta(days=monday.weekday())).replace(hour=9, minute=1, second=0, microsecond=0)
    out = report.run_weekly_reports(now=monday.astimezone(timezone.utc))
    assert out["sent"] == 0


def test_vision_status_and_guards(monkeypatch):
    for key in ("VISION_MODEL", "VISION_API_KEY", "VISION_BASE_URL", "LLM_FAST_API_KEY",
                "OPENAI_API_KEY", "KIMI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    status = vision.status()
    assert status["configured"] is False and "VISION_MODEL" in status["missing"]
    assert vision.has_vision() is False

    with pytest.raises(Exception) as bad:
        vision.analyze_food(b"GIF89a-not-supported")
    assert getattr(bad.value, "code", "") == "bad_image_type"

    monkeypatch.setenv("VISION_MODEL", "fake-vl")
    monkeypatch.setenv("VISION_API_KEY", "fake-key")
    assert vision.has_vision() is True
    with pytest.raises(Exception) as empty:
        vision.analyze_food(b"")
    assert getattr(empty.value, "code", "") == "empty_image"


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeClient:
    payload = json.dumps({"items": [
        {"name": "牛肉面", "portion": "一碗", "kcal": 600, "protein_g": 28, "confidence": 0.8},
        {"name": "", "kcal": 10},
        {"name": "可乐", "kcal": 9999, "confidence": 4},
    ], "note": "汤底油多，估算偏高"})

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return FakeResponse(self.payload)


def test_vision_parses_candidates_without_storing(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "fake-vl")
    monkeypatch.setenv("VISION_API_KEY", "fake-key")
    monkeypatch.setattr(vision.httpx, "Client", FakeClient)
    out = vision.analyze_food(b"\xff\xd8\xff\xe0" + b"0" * 32)
    assert out["needs_confirm"] is True
    assert [item["name"] for item in out["items"]] == ["牛肉面", "可乐"]
    assert out["items"][1]["kcal"] == 3000  # clamped, never trusted blindly
    assert "估算" in out["note"]


def test_chat_rest_timer_returns_a_card(uid, monkeypatch):
    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **k: {
        "args": {"seconds": 60, "exercise": "深蹲", "set_index": 2, "sets": 4},
    })
    out = companion_chat.respond(uid, "companion.rest_timer", "组间休息 60 秒")
    assert out["rest_card"]["seconds"] == 60
    assert out["rest_card"]["label"] == "第 2 / 4 组"
    assert "休息 60 秒" in out["messages"][0]["content"]


def test_api_rest_timer_and_report(client, auth, uid):
    headers = auth(uid)
    store.set_body(uid, height_cm=170, weight_kg=70)
    card = client.post("/api/companion/rest-timer", headers=headers, json={
        "user_id": uid, "seconds": 45, "exercise": "硬拉", "set_index": 1, "sets": 3,
    })
    assert card.status_code == 200
    assert card.json()["data"]["card"]["seconds"] == 45

    preview = client.get("/api/companion/report?days=7", headers=headers)
    assert preview.status_code == 200
    assert "本周周报" in preview.json()["data"]["title"]

    sent = client.post("/api/companion/report", headers=headers, json={"user_id": uid, "days": 7})
    assert sent.status_code == 200
    assert sent.json()["data"]["delivery"].get("reason") in {"no_binding", None}
    items = [i for i in proactive_store.list_pending(uid, status=None, limit=50)
             if i["kind"] == "companion_weekly"]
    assert items


def test_api_food_analyze_is_closed_without_vision(client, auth, uid, monkeypatch):
    for key in ("VISION_MODEL", "VISION_API_KEY", "LLM_FAST_API_KEY", "OPENAI_API_KEY",
                "KIMI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    closed = client.post("/api/companion/food/analyze", headers=auth(uid),
                         files={"file": ("a.jpg", b"\xff\xd8\xff\xe0" + b"0" * 16, "image/jpeg")})
    assert closed.status_code == 501
    assert closed.json()["detail"]["code"] == "vision_not_configured"

    monkeypatch.setattr(vision, "analyze_food", lambda image, mime="": {
        "ok": True, "items": [{"name": "牛肉面", "kcal": 600, "confidence": 0.8}],
        "note": "", "needs_confirm": True, "model": "fake",
    })
    ok = client.post("/api/companion/food/analyze", headers=auth(uid),
                     files={"file": ("a.jpg", b"\xff\xd8\xff\xe0" + b"0" * 16, "image/jpeg")})
    assert ok.status_code == 200
    assert ok.json()["data"]["items"][0]["name"] == "牛肉面"
