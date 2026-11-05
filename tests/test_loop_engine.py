"""Budget config, token metering and the observe → plan → tool → validate engine."""

from __future__ import annotations

import time

import pytest

from app.agent import token_meter
from app.agent.loop import (
    LoopBudget,
    check_budget,
    default_loop_fields,
    progress_report,
    resume_loop_fields,
)
from app.agent.task_loop import Plan, Validation, run_task_loop


def test_budget_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    monkeypatch.setenv("AGENT_MAX_RETRIES", "4")
    monkeypatch.setenv("AGENT_MAX_REDRAFTS", "3")
    monkeypatch.setenv("AGENT_MAX_SECONDS", "12.5")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "999")
    monkeypatch.setenv("AGENT_COST_PER_1K_TOKENS", "0.02")
    monkeypatch.setenv("AGENT_MAX_COST", "1.5")
    b = LoopBudget.from_env()
    assert (b.max_steps, b.max_retries, b.max_redrafts) == (7, 4, 3)
    assert b.max_seconds == 12.5 and b.max_tokens == 999
    assert b.cost_of(2000) == pytest.approx(0.04)
    assert b.max_cost == 1.5


def test_legacy_cost_env_alias(monkeypatch):
    monkeypatch.setenv("AGENT_COST_SOFT_LIMIT", "3")
    assert LoopBudget.from_env().max_cost == 3.0


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"loop_step": 5}, "max_steps"),
        ({"loop_tokens": 100}, "max_tokens"),
        ({"loop_cost_units": 1.0}, "max_cost"),
        ({"loop_started_at": time.time() - 100}, "max_seconds"),
    ],
)
def test_check_budget_each_dimension(patch, reason):
    st = default_loop_fields()
    st["loop_budget"] = LoopBudget(
        max_steps=5, max_seconds=50, max_tokens=100, max_cost=1.0
    ).as_dict()
    assert check_budget(st) == (True, None)
    assert check_budget({**st, **patch}) == (False, reason)


def test_zero_disables_dimension():
    st = default_loop_fields()
    st["loop_budget"] = LoopBudget(max_steps=0, max_seconds=0, max_tokens=0).as_dict()
    st.update(loop_step=10_000, loop_tokens=10**9, loop_started_at=1.0)
    assert check_budget(st) == (True, None)


def test_resume_resets_wall_clock_only():
    patch = resume_loop_fields()
    assert set(patch) == {"loop_started_at", "loop_stop_reason"}
    assert time.time() - patch["loop_started_at"] < 2


def test_progress_report_is_warm_and_honest():
    st = {
        "action": "draft",
        "draft_request": "帮我写一封请假邮件",
        "profile": {"name": "李明"},
        "loop_budget": LoopBudget(max_tokens=500).as_dict(),
    }
    text = progress_report(st, "max_tokens")
    assert "500" in text
    assert "读取了你的档案" in text and "已经完成" in text
    assert "写出草稿" in text  # left to do
    assert "没有任何内容被自动发出" in text


def test_token_estimate_and_nested_meter():
    assert token_meter.estimate_tokens("") == 0
    assert token_meter.estimate_tokens("你好世界") == 4
    assert token_meter.estimate_tokens("abcdefgh") == 2
    with token_meter.metered() as outer:
        with token_meter.metered() as inner:
            token_meter.record_completion("系统", "用户", "回答")
        assert inner.tokens > 0
    assert outer.tokens == inner.tokens


def test_gateway_records_tokens(monkeypatch):
    from app.gateway import llm_gateway

    from app.gateway.core import Result

    # Gateway internals after merging PR #7 (core.complete) with PR #8 (token metering).
    monkeypatch.setattr(
        llm_gateway.core, "complete", lambda *a, **k: Result(content="好的" * 10)
    )
    monkeypatch.setattr(llm_gateway, "_complete_json", lambda *a, **k: {"ok": True})
    with token_meter.metered() as m:
        llm_gateway.chat_text("sys", "用户说的话")
        llm_gateway.chat_json("sys", "json please")
    assert m.calls == 2
    assert m.tokens >= 20


def _loop(state, validate_results, **kw):
    seen_feedback: list[list[str]] = []
    calls = {"act": 0}
    tool = kw.pop("tool", "draft")

    def observe(ctx):
        seen_feedback.append(ctx.reasons())
        return {"n": ctx.attempt}

    def plan(ctx, obs):
        return Plan(tool=tool, args=obs)

    def act(ctx, p):
        calls["act"] += 1
        token_meter.record_completion("s" * 40, "u" * 40, "o" * 40)
        return {"attempt": p.args["n"]}

    results = iter(validate_results)

    def validate(ctx, p, out):
        ok, reason = next(results)
        return Validation.passed() if ok else Validation.failed(reason)

    res = run_task_loop(
        name="t", state=state, observe=observe, plan=plan, act=act, validate=validate, **kw
    )
    return res, seen_feedback, calls


def test_retry_carries_reason_then_succeeds():
    res, seen, calls = _loop(default_loop_fields(), [(False, "太长了"), (True, "")])
    assert res.status == "done" and res.ok
    assert res.attempts == 2 and calls["act"] == 2
    assert seen == [[], ["太长了"]]
    assert res.output == {"attempt": 2}
    assert res.tokens > 0
    patch = res.loop_patch(default_loop_fields())
    assert patch["loop_step"] == 2 and patch["loop_tokens"] == res.tokens


def test_retries_capped_by_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_RETRIES", "1")
    res, seen, calls = _loop(default_loop_fields(), [(False, "a"), (False, "b"), (True, "")])
    assert res.status == "exhausted"
    assert calls["act"] == 2
    assert [f["reason"] for f in res.feedback] == ["a", "b"]


def test_seed_feedback_from_user_reject():
    st = {**default_loop_fields(), "loop_feedback": [{"source": "user_reject", "reason": "太浮夸"}]}
    res, seen, _ = _loop(st, [(True, "")])
    assert seen == [["太浮夸"]]


def test_budget_stop_on_tokens_is_graceful(monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    st = default_loop_fields()
    st["loop_budget"] = LoopBudget(max_tokens=30).as_dict()
    res, _, calls = _loop(st, [(False, "x")] * 5)
    assert res.status == "stopped" and res.stop_reason == "max_tokens"
    assert calls["act"] == 1
    assert "先停一下" in res.message and "已尝试 1 次" in res.message


def test_budget_stop_on_steps(monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    st = {**default_loop_fields(), "loop_step": 3}
    st["loop_budget"] = LoopBudget(max_steps=4).as_dict()
    res, _, calls = _loop(st, [(False, "x")] * 5)
    assert res.stop_reason == "max_steps" and calls["act"] == 1


def test_budget_stop_on_wall_clock(monkeypatch):
    monkeypatch.setenv("AGENT_TIER_ESCALATION", "0")
    st = {**default_loop_fields(), "loop_started_at": time.time() - 10}
    st["loop_budget"] = LoopBudget(max_seconds=5).as_dict()
    res, _, calls = _loop(st, [(True, "")])
    assert res.stop_reason == "max_seconds" and calls["act"] == 0


def test_external_tool_blocked_without_confirm():
    res, _, calls = _loop(default_loop_fields(), [(True, "")], tool="send_message")
    assert res.status == "blocked" and calls["act"] == 0
    assert "确认" in res.message


def test_external_tool_allowed_after_confirm():
    st = {**default_loop_fields(), "confirmed": True}
    res, _, calls = _loop(st, [(True, "")], tool="send_greeting")
    assert res.status == "done" and calls["act"] == 1


def test_tool_exception_becomes_retry_reason():
    attempts = {"n": 0}

    def act(ctx, p):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("timeout")
        return "ok"

    res = run_task_loop(
        name="t",
        state=default_loop_fields(),
        observe=lambda ctx: {},
        plan=lambda ctx, obs: Plan(tool="x"),
        act=act,
        validate=lambda ctx, p, out: Validation.passed(),
    )
    assert res.ok and res.attempts == 2
    assert "timeout" in res.feedback[0]["reason"]
