"""Tiered budgets, one-shot escalation, runaway detection, wrap-up, continue, cost caps."""

from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent import cost_ledger, runaway, token_meter
from app.agent.budget_tiers import (
    HARD,
    MEDIUM,
    SIMPLE,
    TASK_DIFFICULTY,
    classify_difficulty,
    continue_extension,
    tier_limits,
)
from app.agent.harness.traces import list_traces
from app.agent.loop import (
    LoopBudget,
    check_budget,
    check_budget_or_escalate,
    continue_card,
    default_loop_fields,
    escalate_tier,
    extend_budget,
    near_limit,
)
from app.agent.node_wrap import wrap_node
from app.agent.subgraphs import draft_confirm
from app.agent.task_loop import Plan, Validation, run_task_loop
from app.gateway.llm_gateway import route
from app.graph import continue_task, get_thread_snapshot, run_action, task_tier
from app.llm import ModelTier
from tests.conftest import DRAFT_REQUEST

SHANGHAI = cost_ledger.ZoneInfo("Asia/Shanghai")


@pytest.fixture
def fake_drafts(monkeypatch):
    calls: list[int] = []

    def fake_draft(request, profile, memory="", feedback=None):
        calls.append(1)
        token_meter.record_completion("sys", request, "draft")
        return {"kind": "email", "title": "请假邮件", "body": "您好：\n周五想请一天假，谢谢。\n李雷"}

    monkeypatch.setattr(draft_confirm, "draft_text", fake_draft)
    return calls


def _draft(uid, **extra):
    """A writing request: the medium-tier draft → confirm task."""
    return run_action({"user_id": uid, "action": "draft", "message": DRAFT_REQUEST,
                       "draft_request": DRAFT_REQUEST, **extra})


# ---- tiers ------------------------------------------------------------------


def test_tier_defaults_match_founder_numbers():
    assert tier_limits(SIMPLE) == {"max_steps": 15, "max_seconds": 180.0, "max_tokens": 100_000, "max_cost": 0.0}
    assert tier_limits(MEDIUM) == {"max_steps": 50, "max_seconds": 900.0, "max_tokens": 400_000, "max_cost": 0.0}
    assert tier_limits(HARD) == {"max_steps": 120, "max_seconds": 2700.0, "max_tokens": 1_500_000, "max_cost": 0.0}


def test_tier_env_overrides_and_legacy_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_BUDGET_HARD_TOKENS", "2000000")
    monkeypatch.setenv("AGENT_BUDGET_SIMPLE_SECONDS", "60")
    monkeypatch.setenv("AGENT_MAX_STEPS", "33")  # legacy → medium only
    monkeypatch.setenv("AGENT_MAX_TOKENS", "70000")
    monkeypatch.setenv("AGENT_BUDGET_MEDIUM_TOKENS", "450000")  # tier env wins over legacy
    assert LoopBudget.from_env(HARD).max_tokens == 2_000_000
    assert LoopBudget.from_env(SIMPLE).max_seconds == 60.0
    assert LoopBudget.from_env(MEDIUM).max_steps == 33
    assert LoopBudget.from_env(MEDIUM).max_tokens == 450_000
    assert LoopBudget.from_env(HARD).max_steps == 120
    assert LoopBudget.from_env(SIMPLE).max_steps == 15


def test_difficulty_mapping_and_model_routing_share_one_classifier():
    for task in ("chat", "update_profile", "confirm", "summary", "memory_extract",
                 "memory_reconcile", "risk_classify", "tool_selection"):
        assert classify_difficulty(task) == SIMPLE
    for task in ("draft", "draft_text"):
        assert classify_difficulty(task) == MEDIUM
    for task in ("strong", "reason"):
        assert classify_difficulty(task) == HARD
    assert classify_difficulty("unknown-task") == SIMPLE
    assert classify_difficulty("DRAFT ") == MEDIUM
    for task in ("chat", "draft", "draft_text", "strong", "reason", "unknown-task"):
        expect = ModelTier.STRONG if classify_difficulty(task) == HARD else ModelTier.FAST
        assert route(task) == expect


def test_run_action_seeds_tier_from_task(uid, fake_drafts):
    assert run_action({"user_id": uid, "action": "chat", "message": "你好"})["loop"]["tier"] == SIMPLE
    assert task_tier({"user_id": uid, "action": "draft", "draft_request": DRAFT_REQUEST}) == MEDIUM
    assert task_tier({"user_id": uid, "action": "update_profile", "profile": {"city": "上海"}}) == SIMPLE
    assert task_tier({"user_id": uid, "action": "chat", "message": DRAFT_REQUEST}) == SIMPLE
    out = _draft(uid)
    assert out["awaiting_confirm"] is True and fake_drafts == [1]
    assert out["loop"]["tier"] == MEDIUM
    assert out["loop"]["budget"]["max_tokens"] == 400_000


# ---- escalation ---------------------------------------------------------------


def test_escalation_happens_once_and_is_traced():
    st = {**default_loop_fields(SIMPLE), "thread_id": "esc-t1", "action": "chat"}
    patch = escalate_tier(st, reason="test")
    assert patch["loop_tier"] == MEDIUM and patch["loop_escalated"] is True
    assert patch["loop_budget"]["max_steps"] == 50
    assert escalate_tier({**st, **patch}, reason="again") == {}
    rows = [r for r in list_traces("esc-t1") if r["tool"] == "loop_escalate"]
    assert len(rows) == 1 and rows[0]["detail"]["from"] == SIMPLE and rows[0]["detail"]["to"] == MEDIUM


def test_hard_tier_cannot_escalate_and_env_can_disable(monkeypatch):
    assert escalate_tier(default_loop_fields(HARD), reason="x") == {}
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    assert escalate_tier(default_loop_fields(SIMPLE), reason="x") == {}


def test_budget_overrun_escalates_then_stops():
    st = {**default_loop_fields(SIMPLE), "loop_step": 15}
    ok, reason, esc = check_budget_or_escalate(st)
    assert ok and reason is None and esc["loop_tier"] == MEDIUM
    st = {**st, **esc, "loop_step": 50}
    ok, reason, esc2 = check_budget_or_escalate(st)
    assert not ok and reason == "max_steps" and esc2 == {}


def test_task_loop_escalates_near_limit_and_reports_it():
    st = {**default_loop_fields(SIMPLE), "loop_step": 13}

    def act(ctx, p):
        return {"n": ctx.attempt}

    res = run_task_loop(
        name="t",
        state=st,
        observe=lambda ctx: {"n": ctx.attempt},
        plan=lambda ctx, obs: Plan(tool="draft", args=obs),
        act=act,
        validate=lambda ctx, p, out: Validation.passed(),
    )
    assert res.ok
    patch = res.loop_patch(st)
    assert patch["loop_tier"] == MEDIUM and patch["loop_escalated"] is True
    assert patch["loop_escalation"]["reason"].startswith("budget:near:max_steps")


def test_draft_graph_escalates_once_when_budget_runs_out(uid, fake_drafts):
    # A medium task that already used its steps moves up one tier instead of stopping.
    out = _draft(uid, loop_step=50)
    assert out["awaiting_confirm"] is True and fake_drafts == [1]
    assert out["loop"]["tier"] == HARD and out["loop"]["escalated"] is True
    assert out["loop"]["escalation"]["reason"].startswith("budget:max_steps")
    assert any(r["tool"] == "loop_escalate" for r in list_traces(out["thread_id"]))


# ---- runaway detection ----------------------------------------------------------


def _e(tool, args=None, **kw):
    return runaway.make_entry(tool, args, **kw)


def test_runaway_patterns():
    same = [_e("search", {"q": 1}, progress=True)] * 3
    assert runaway.detect(same)[0] == "repeat_call"
    errs = [_e("t", {"i": i}, error="超时", progress=True) for i in range(3)]
    assert runaway.detect(errs)[0] == "repeat_error"
    idle = [_e("t", {"i": i}) for i in range(5)]
    assert runaway.detect(idle)[0] == "no_progress"
    osc = [_e("a" if i % 2 == 0 else "b", {"x": i % 2}, progress=True) for i in range(6)]
    assert runaway.detect(osc)[0] == "oscillation"
    assert runaway.detect([_e("submit_form", risk=True, progress=True)])[0] == "risk_signal"
    fine = [_e("t", {"i": i}, progress=True) for i in range(10)]
    assert runaway.detect(fine) is None
    assert runaway.detect([_e("t", {"i": i}) for i in range(4)]) is None


def test_task_loop_stops_on_same_call_three_times():
    calls = {"act": 0}

    def act(ctx, p):
        calls["act"] += 1
        return {"same": True}

    res = run_task_loop(
        name="搜索",
        state=default_loop_fields(HARD),
        observe=lambda ctx: {},
        plan=lambda ctx, obs: Plan(tool="search", args={"q": "同一个"}),
        act=act,
        validate=lambda ctx, p, out: Validation.failed(f"不行{ctx.attempt}"),
        max_attempts=10,
    )
    assert res.status == "runaway" and res.stop_reason == "runaway:repeat_call"
    assert calls["act"] == 3
    assert res.stop_error == "loop_runaway:repeat_call"
    assert "先停一下" in res.message


def test_task_loop_stops_on_same_error_three_times():
    res = run_task_loop(
        name="t",
        state=default_loop_fields(HARD),
        observe=lambda ctx: {"n": ctx.attempt},
        plan=lambda ctx, obs: Plan(tool="fetch", args=obs),
        act=lambda ctx, p: {"n": p.args["n"]},
        validate=lambda ctx, p, out: Validation.failed("页面打不开"),
        max_attempts=10,
    )
    assert res.status == "runaway" and res.stop_reason == "runaway:repeat_error"
    assert res.attempts == 3


def test_captcha_in_external_task_stops_immediately():
    res = run_task_loop(
        name="提交",
        state={**default_loop_fields(HARD), "confirmed": True},
        observe=lambda ctx: {},
        plan=lambda ctx, obs: Plan(tool="submit_form", args={"form": 1}),
        act=lambda ctx, p: {"page": "请完成安全验证：拖动滑块"},
        validate=lambda ctx, p, out: Validation.passed(),
    )
    assert res.status == "runaway" and res.stop_reason == "runaway:risk_signal"
    assert "验证码" in res.message and res.attempts == 1


def test_node_wrapper_stops_repeated_identical_node():
    node = wrap_node("probe", lambda state: {"messages": [{"role": "assistant", "content": "x"}]})
    state = {**default_loop_fields(HARD), "thread_id": "rw-1", "action": "chat", "message": "hi"}
    for _ in range(2):
        out = node(state)
        assert not str(out.get("error") or "")
        state = {**state, **out}
    out = node(state)
    assert out["error"] == "loop_runaway:repeat_call"
    assert any(r["tool"] == "loop_runaway" for r in list_traces("rw-1"))


def test_node_wrapper_risk_signal_only_for_external_nodes():
    def page(state):
        return {"error": "访问过于频繁，请输入验证码"}

    st = {**default_loop_fields(HARD), "thread_id": "rw-2", "action": "draft"}
    assert wrap_node("submit_form", page)(st)["error"] == "loop_runaway:risk_signal"
    assert wrap_node("draft", page)(st)["error"] == "访问过于频繁，请输入验证码"


# ---- near-limit wrap-up + continue ------------------------------------------------


def test_near_limit_rules():
    assert near_limit({**default_loop_fields(HARD), "loop_step": 96}) == "max_steps"  # 80%
    assert near_limit({**default_loop_fields(HARD), "loop_step": 95}) is None
    st = default_loop_fields(HARD)
    st["loop_budget"] = {**st["loop_budget"], "max_steps": 6}
    assert near_limit({**st, "loop_step": 4}) == "max_steps"  # 2 left
    assert near_limit({**default_loop_fields(HARD), "loop_tokens": 1_200_000}) == "max_tokens"


def test_task_loop_wraps_up_with_partial_output(monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    st = {**default_loop_fields(HARD), "loop_step": 95}
    res = run_task_loop(
        name="写作草稿",
        state=st,
        observe=lambda ctx: {"n": ctx.attempt},
        plan=lambda ctx, obs: Plan(tool="draft_text", args=obs),
        act=lambda ctx, p: {"draft": p.args["n"]},
        validate=lambda ctx, p, out: Validation.failed("还能更好"),
    )
    assert res.status == "wrapped_up" and res.output == {"draft": 1}
    assert res.loop_patch(st)["loop_wrap_up"] is True
    assert "快到上限" in res.message


def test_graph_stops_at_budget_with_continue_card(uid, fake_drafts, monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    monkeypatch.setenv("AGENT_BUDGET_MEDIUM_STEPS", "4")
    out = _draft(uid, loop_step=4)
    state = out["state"]
    assert state["error"] == "loop_budget:max_steps" and fake_drafts == []
    assert not state.get("draft") and out["awaiting_confirm"] is False
    card = out["loop"]["continue_card"]
    assert card["type"] == "continue_budget" and card["kind"] == "budget" and card["can_continue"]
    assert card["tier"] == MEDIUM
    assert any("写出草稿" in t for t in card["todo"])
    assert card["spent"]["steps"] == 4 and "cost_cny" in card["spent"]
    assert card["extension"]["steps"] == 2 and card["estimate_cny"] >= 0
    msg = state["messages"][-1]["content"]
    assert "先停一下" in msg and "继续" in msg and "没有任何内容被自动发出" in msg
    assert "4 步" in msg


def test_continue_adds_half_tier_and_finishes_the_draft(uid, fake_drafts, monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    monkeypatch.setenv("AGENT_BUDGET_MEDIUM_STEPS", "4")
    tid = _draft(uid, loop_step=4)["thread_id"]
    assert fake_drafts == []
    o1 = continue_task(tid, uid)
    assert o1["status"] == "continued" and o1["loop"]["budget"]["max_steps"] == 6
    assert o1["loop"]["continues"] == 1
    assert o1["awaiting_confirm"] is True and fake_drafts == [1]  # finished the draft
    assert o1["state"]["draft"]["body"] and o1["state"]["draft"]["kind"] == "email"
    assert o1["state"]["draft_request"] == DRAFT_REQUEST
    tools = [r["tool"] for r in list_traces(tid, limit=500)]
    assert tools.count("loop_continue") == 1
    # Each further click adds 50% of the tier's base again, without limit.
    st = {**default_loop_fields(MEDIUM), "loop_budget": o1["loop"]["budget"]}
    assert extend_budget(st)["max_steps"] == 8
    monkeypatch.setenv("AGENT_BUDGET_MEDIUM_STEPS", "40")  # bigger tier → bigger click
    assert extend_budget({**st, "loop_budget": {**o1["loop"]["budget"], "max_steps": 8}})["max_steps"] == 28


def test_extension_is_half_of_tier():
    ext = continue_extension(HARD)
    assert ext["max_steps"] == 60 and ext["max_seconds"] == 1350.0 and ext["max_tokens"] == 750_000
    st = default_loop_fields(MEDIUM)
    assert extend_budget(st)["max_tokens"] == 600_000


def test_continue_rejects_held_lock_and_releases_after_error(uid):
    from app import graph

    tid = f"missing-{uid}"
    lock = threading.Lock()
    with graph._continue_locks_guard:
        graph._continue_locks[tid] = lock
    with lock:
        with pytest.raises(graph.ContinueError) as exc:
            continue_task(tid, uid)
        assert exc.value.code == "continue_in_progress"
        assert exc.value.http_status == 409
        assert lock.locked()
    with pytest.raises(graph.ContinueError) as exc:
        continue_task(tid, uid)
    assert exc.value.code == "thread_not_found"
    assert not lock.locked()


def test_continue_releases_lock_after_cost_cap_return(uid, fake_drafts):
    from app import graph

    cost_ledger.record_spend(uid, 2.5)
    tid = _draft(uid)["thread_id"]
    assert continue_task(tid, uid)["status"] == "cost_cap"
    assert not graph._continue_locks[tid].locked()


@pytest.mark.parametrize("elapsed", [2700.0, 3000.0, 1000.0])
def test_continue_preserves_remaining_time_plus_half_tier(uid, monkeypatch, elapsed):
    from app import graph

    clock = [10_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    state = {
        **default_loop_fields(HARD),
        "user_id": uid,
        "loop_started_at": clock[0] - elapsed,
        "error": "loop_budget:max_seconds",
        "loop_task_payload": {"action": "draft", "draft_request": DRAFT_REQUEST},
    }
    state["pending_continue"] = continue_card(state, "max_seconds")
    monkeypatch.setattr(graph, "get_thread_snapshot", lambda tid: {
        "exists": True, "user_id": uid, "values": state, "next": [],
    })
    monkeypatch.setattr(graph, "get_graph", lambda: SimpleNamespace(
        invoke=lambda payload, config: payload,
    ))
    monkeypatch.setattr(graph, "_pack", lambda tid, result, g, config: {"state": result})
    clock[0] += 3600.0  # Waiting to click continue does not consume active time.
    result = continue_task(f"time-{uid}", uid)["state"]
    assert result["draft_request"] == DRAFT_REQUEST
    tier_time = tier_limits(HARD)["max_seconds"]
    remaining = result["loop_budget"]["max_seconds"] - (clock[0] - result["loop_started_at"])
    expected = max(0.0, tier_time - elapsed) + 0.5 * tier_time
    assert remaining == pytest.approx(expected)
    clock[0] += expected - 0.01
    assert check_budget(result) == (True, None)
    clock[0] += 0.02
    assert check_budget(result) == (False, "max_seconds")
    assert not graph._continue_locks[f"time-{uid}"].locked()


def test_continue_rejects_other_user_and_non_stopped_threads(uid, fake_drafts):
    from app.graph import ContinueError

    tid = _draft(uid)["thread_id"]  # awaiting confirm, not stopped
    with pytest.raises(ContinueError) as e:
        continue_task(tid, "someone-else")
    assert e.value.http_status == 403
    with pytest.raises(ContinueError) as e:
        continue_task(tid, uid)
    assert e.value.code == "awaiting_confirm"


def test_api_continue_requires_auth_and_ownership(uid, fake_drafts, auth, monkeypatch):
    from app.main import app

    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    monkeypatch.setenv("AGENT_BUDGET_MEDIUM_STEPS", "4")
    from tests.conftest import RawTestClient

    client = RawTestClient(app)
    tid = _draft(uid, loop_step=4)["thread_id"]
    assert client.post("/api/continue", json={"thread_id": tid}).status_code == 401
    r = client.post("/api/continue", json={"thread_id": tid}, headers=auth("intruder"))
    assert r.status_code == 404
    r = client.post("/api/continue", json={"thread_id": tid}, headers=auth(uid))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "continued" and body["data"]["loop"]["continues"] == 1
    assert body["awaiting_confirm"] is True and fake_drafts == [1]


# ---- per-user cost caps -----------------------------------------------------------


def test_record_spend_is_atomic_across_threads(uid):
    cost_ledger._ensure()
    barrier = threading.Barrier(8)
    now = datetime(2026, 10, 16, 15, 30, tzinfo=SHANGHAI)

    def spend():
        barrier.wait()
        for _ in range(25):
            cost_ledger.record_spend(uid, 0.01, now=now)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(spend) for _ in range(8)]
        for future in futures:
            future.result()
    assert round(cost_ledger.usage(uid, now=now)["today"], 4) == 2.0


def test_cap_defaults_and_env(uid, monkeypatch):
    assert cost_ledger.caps_for("free") == {"daily": 2.0, "monthly": 30.0}
    assert cost_ledger.caps_for("paid") == {"daily": 20.0, "monthly": 300.0}
    monkeypatch.setenv("AGENT_COST_CAP_PAID_DAILY", "50")
    assert cost_ledger.caps_for("paid")["daily"] == 50.0
    assert cost_ledger.plan_for(uid) == "free"
    monkeypatch.setenv("AGENT_PAID_USERS", f"x,{uid}")
    assert cost_ledger.plan_for(uid) == "paid"
    cost_ledger.set_plan(uid, "free")
    assert cost_ledger.plan_for(uid) == "free"


def test_daily_and_monthly_caps_with_reset_times(uid):
    now = datetime(2026, 10, 16, 15, 30, tzinfo=SHANGHAI)
    cost_ledger.record_spend(uid, 1.5, now=now)
    assert cost_ledger.check(uid, now=now).ok
    cost_ledger.record_spend(uid, 0.6, now=now)
    st = cost_ledger.check(uid, now=now)
    assert not st.ok and st.scope == "daily"
    assert st.reset_at.startswith("2026-10-17T00:00:00")
    assert "明天 00:00" in cost_ledger.cap_message(st) and "继续" in cost_ledger.cap_message(st)
    for d in range(1, 15):
        cost_ledger.record_spend(uid, 2.0, now=now.replace(day=d))
    later = now.replace(day=20)
    st = cost_ledger.check(uid, now=later)
    assert not st.ok and st.scope == "monthly" and st.reset_at.startswith("2026-11-01T00:00:00")
    assert cost_ledger.check(uid, now=datetime(2026, 11, 1, 0, 1, tzinfo=SHANGHAI)).ok


def test_node_spend_is_recorded(uid, fake_drafts):
    _draft(uid)
    assert cost_ledger.usage(uid)["today"] > 0


def test_cost_cap_stops_task_saves_checkpoint_and_resumes_after_reset(uid, fake_drafts, monkeypatch):
    cost_ledger.record_spend(uid, 2.5)
    out = _draft(uid)
    state = out["state"]
    assert state["error"] == "loop_budget:cost_cap" and fake_drafts == []
    assert "额度" in state["messages"][-1]["content"] and "恢复" in state["messages"][-1]["content"]
    card = out["loop"]["continue_card"]
    assert card["kind"] == "cost_cap" and card["can_continue"] is False and card["resume_after"]
    snap = get_thread_snapshot(out["thread_id"])
    assert snap["values"]["loop_task_payload"]["draft_request"] == DRAFT_REQUEST

    still = continue_task(out["thread_id"], uid)
    assert still["status"] == "cost_cap" and fake_drafts == []

    monkeypatch.setenv("AGENT_COST_CAP_FREE_DAILY", "100")  # cap reset / raised
    before = dict(snap["values"]["loop_budget"])
    resumed = continue_task(out["thread_id"], uid)
    assert resumed["status"] == "continued" and resumed["awaiting_confirm"] is True
    assert resumed["loop"]["budget"]["max_steps"] == before["max_steps"]  # no free extension
    assert fake_drafts == [1]


def test_crisis_message_bypasses_cost_cap(uid):
    from app.agent.subgraphs.chat import HOTLINE_MARK

    cost_ledger.record_spend(uid, 10.0)
    out = run_action({"user_id": uid, "action": "chat", "message": "这周太难熬了，我不想活了"})
    assert not str(out["state"].get("error") or "").startswith("loop_budget")
    assert HOTLINE_MARK in out["state"]["messages"][-1]["content"]
    normal = run_action({"user_id": uid, "action": "chat", "message": "帮我看看周末怎么安排"})
    assert normal["state"]["error"] == "loop_budget:cost_cap"
    draft = _draft(uid)
    assert draft["state"]["error"] == "loop_budget:cost_cap" and not draft["state"].get("draft")


def test_task_loop_respects_cost_cap(uid):
    cost_ledger.record_spend(uid, 3.0)
    calls = {"act": 0}

    def act(ctx, p):
        calls["act"] += 1
        return {}

    res = run_task_loop(
        name="t",
        state={**default_loop_fields(MEDIUM), "user_id": uid},
        observe=lambda ctx: {},
        plan=lambda ctx, obs: Plan(tool="draft_text"),
        act=act,
        validate=lambda ctx, p, out: Validation.passed(),
    )
    assert res.status == "stopped" and res.stop_reason == "cost_cap:daily"
    assert res.stop_error == "loop_budget:cost_cap" and calls["act"] == 0
    assert res.card["kind"] == "cost_cap"


def test_confirm_wait_still_excluded_from_time_budget(uid, fake_drafts, monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    monkeypatch.setenv("AGENT_BUDGET_MEDIUM_SECONDS", "2")
    out = _draft(uid)
    assert out["awaiting_confirm"] is True
    time.sleep(2.1)
    o2 = run_action({"user_id": uid, "_resume": {"confirmed": False, "reason": "短一点"}}, thread_id=out["thread_id"])
    assert not str(o2["state"].get("error") or "").startswith("loop_budget")
    assert o2["awaiting_confirm"] is True
    assert fake_drafts == [1, 1]
    assert {"source": "user_reject", "reason": "短一点"} in o2["state"]["loop_feedback"]
