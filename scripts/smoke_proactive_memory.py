#!/usr/bin/env python3
"""Smoke: proactive scheduler (user schedules) + persistent memory cross-thread.

Offline by default (clears LLM keys). Enables short-interval proactive ticks.

  .venv/bin/python scripts/smoke_proactive_memory.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmpdir = tempfile.mkdtemp(prefix="personal_agent_proactive_")
os.environ["DATA_DIR"] = tmpdir
os.environ["PROACTIVE_ENABLED"] = "1"
os.environ["PROACTIVE_TZ"] = "Asia/Shanghai"
os.environ["PROACTIVE_TICK_SECONDS"] = "2"
os.environ["PROACTIVE_MAX_USERS_PER_TICK"] = "10"
os.environ["PROACTIVE_SOFT_BUDGET_SECONDS"] = "20"
os.environ.setdefault("AUTH_SECRET", "smoke-proactive-" + os.urandom(16).hex())

os.environ["PYTHON_DOTENV_DISABLED"] = "1"  # a filled .env must not re-add the keys below
for _k in (
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "LLM_FAST_API_KEY",
    "LLM_STRONG_API_KEY",
):
    os.environ.pop(_k, None)

from app import db
from app.agent import proactive as proactive_mod
from app.agent.memory import (
    format_memory_context,
    get_profile,
    search_events,
    write_event,
)
from app.agent.memory.summary import flush_summaries, maybe_summarize_turn
from app.agent.schedules import service as schedules
from app.agent.schedules.models import Schedule
from app.auth import mint_user_token
from app.data_access import user_scope
from app.data_access.session import SessionLocal
from app.graph import run_action
from app.main import app
from fastapi.testclient import TestClient

MEM_USER = "mem_smoke_user"
PROACTIVE_USER = "proactive_smoke_user"


def _ok(name: str) -> None:
    print(f"[PASS] {name}")


def _headers(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_user_token(uid)}"}


def test_memory_cross_thread() -> None:
    uid = MEM_USER
    db.init_db()
    with user_scope(uid):
        db.save_profile(
            uid,
            {
                "name": "记忆测",
                "city": "上海",
                "occupation": "数据分析师",
                "interests": ["跑步", "摄影"],
                "preferences": ["早上九点前不要打扰"],
                "facts": ["下周三去杭州出差"],
            },
        )
        # Thread A: chat writes episodic + summary
        out_a = run_action(
            {"user_id": uid, "action": "chat", "message": "我只在周末跑步，工作日晚上不要安排运动"},
            thread_id="thread-memory-a",
        )
        assert out_a.get("state", {}).get("messages"), out_a
        summ = maybe_summarize_turn(
            uid,
            kind="chat",
            user_text="我只在周末跑步，工作日晚上不要安排运动",
            assistant_text="已记下只在周末跑步的偏好。",
            thread_id="thread-memory-a",
        )
        assert summ.get("ok") and summ.get("scheduled"), summ
        assert flush_summaries(timeout=8), "summary did not finish"
        write_event(
            uid,
            "confirm_accepted",
            "确认了一封请假邮件草稿",
            tags=["confirm", "smoke"],
            thread_id="thread-memory-a",
        )

        events = search_events(uid, limit=20)
        assert events, events
        prof = get_profile(uid)
        assert prof.get("dialogue_summary") or any(e.get("kind") == "summary" for e in events), (prof, events)

        # Thread B: new thread — memory still injected by user_id
        out_b = run_action(
            {"user_id": uid, "action": "chat", "message": "我之前说过什么偏好？"},
            thread_id="thread-memory-b",
        )
        msgs = (out_b.get("state") or {}).get("messages") or []
        assert msgs, out_b
        ctx = format_memory_context(uid, query="跑步", limit=8)
        assert ctx and ("记忆" in ctx or "档案" in ctx or "记忆测" in ctx), ctx

        # Draft path also runs with the same memory (offline draft when no model)
        out_c = run_action(
            {"user_id": uid, "action": "draft", "message": "帮我写一封请假邮件，周五请一天假",
             "draft_request": "帮我写一封请假邮件，周五请一天假", "channel": "web"},
            thread_id="thread-memory-c",
        )
        draft = (out_c.get("state") or {}).get("draft") or {}
        assert out_c.get("awaiting_confirm") and draft.get("body"), out_c
        assert "记忆测" in draft["body"], draft["body"]
    _ok("memory_cross_thread")


def _make_due(uid: str, sid: int) -> None:
    with user_scope(uid), SessionLocal() as session, session.begin():
        schedules.owned(session, uid, sid).next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_proactive_reminder_runs() -> None:
    uid = PROACTIVE_USER
    with user_scope(uid):
        db.save_profile(uid, {"name": "主动测", "city": "深圳", "tz": "Asia/Shanghai"})
        row = schedules.mutate(uid, "create", {
            "name": "烟雾测试提醒",
            "job_kind": "one_shot_reminder",
            "trigger": {"type": "once", "run_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            "prompt_text": "提醒：下午三点交周报",
            "delivery": "in_app",
        })
        assert row["status"] == "active" and row["tz"] == "Asia/Shanghai", row
        _make_due(uid, row["id"])

    # Direct tick (scheduler also started below)
    tick = proactive_mod.trigger_tick_now()
    assert tick.get("ok"), tick
    runs = tick.get("schedules") or []
    assert len(runs) == 1 and runs[0].get("ok") and runs[0].get("item_id"), tick
    assert runs[0]["delivery"]["reason"] == "in_app_only", runs[0]

    with user_scope(uid):
        updated = schedules.get_schedule(uid, row["id"])
        assert updated["status"] == "completed" and updated["run_count"] == updated["success_count"] == 1, updated
        history = schedules.runs(uid, row["id"])
        assert len(history) == 1 and history[0]["ok"], history

    with user_scope(uid):
        items = proactive_mod.store.list_pending(uid, limit=50, status="pending")
    assert any(i.get("kind") == "one_shot_reminder" and i.get("body") == "提醒：下午三点交周报" for i in items), items
    assert all(i.get("payload", {}).get("suggest_only") for i in items if i.get("kind") == "one_shot_reminder"), items
    _ok("proactive_reminder_inbox")

    # Idempotent second tick: the completed one-shot must not run or write again
    tick2 = proactive_mod.trigger_tick_now()
    assert tick2.get("ok") and not tick2.get("schedules"), tick2
    with user_scope(uid):
        after = proactive_mod.store.list_pending(uid, limit=50, status=None)
    assert sum(1 for i in after if i.get("kind") == "one_shot_reminder") == 1, after
    with user_scope(uid):
        assert len(schedules.runs(uid, row["id"])) == 1
        assert len(schedules.list_schedules(uid, "one_shot_reminder")) == 1
        try:
            schedules.list_schedules(uid, "unknown_kind")
            raise AssertionError("unknown job kinds must be rejected")
        except ValueError:
            pass
    _ok("proactive_one_shot_idempotent")


def test_api_surface() -> None:
    # Start scheduler as lifespan would
    st = proactive_mod.start()
    assert st.get("proactive_enabled") is True, st
    client = TestClient(app)
    h = client.get("/api/health").json()
    assert h.get("proactive_enabled") is True, h
    assert "proactive" in h, h
    mem = client.get(f"/api/memory/{PROACTIVE_USER}", headers=_headers(PROACTIVE_USER)).json()
    data = mem.get("data") or {}
    assert "events" in data and "profile" in data and "context" in data, mem
    pend = client.get("/api/proactive/pending", params={"user_id": PROACTIVE_USER},
                      headers=_headers(PROACTIVE_USER)).json()
    pdata = pend.get("data") or {}
    assert "items" in pdata and pdata.get("count") == 1, pend
    listing = client.get("/api/schedules", headers=_headers(PROACTIVE_USER)).json()
    assert listing.get("status") == "schedules", listing
    assert listing["data"]["stats"]["run_count"] == 1, listing
    status = client.get("/api/proactive/status", headers=_headers(PROACTIVE_USER)).json()
    assert (status.get("data") or {}).get("scheduler_alive") is True, status
    _ok("api_health_memory_proactive_schedules")


def main() -> None:
    print("DATA_DIR=", tmpdir)
    db.init_db()
    test_memory_cross_thread()
    test_proactive_reminder_runs()
    test_api_surface()
    proactive_mod.stop()
    print("ALL PASS")


if __name__ == "__main__":
    main()
