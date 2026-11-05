"""Regression tests for the PR #9 benchmark review (P0 / P1 findings)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.data_access import user_scope
from app import db
from app.agent import node_wrap
from app.agent.harness import context as trace_ctx
from app.agent.harness import leak, traces
from app.agent.harness.redact import redact_obj
from app.agent.node_wrap import GUARD_UNAVAILABLE, NODE_FAILED, wrap_node

db.init_db()

CANARY = "sk-review-canary-0123456789abcdef"


def _rows(thread_id):
    return traces.query_traces(thread_id=thread_id, limit=50)["traces"]


# ------------------------------------------------------------------ P0-1 fail closed


def _leaky_node(state):
    return {
        "messages": [{"role": "assistant", "content": f"内部 {CANARY} SANDBOX-VAL-9"}],
        "draft": {"body": "SANDBOX-VAL-9", "nested": {"x": CANARY}},
        "ask": {"question": "SANDBOX-VAL-9"},
    }


def _assert_fail_closed(out, code):
    blob = json.dumps(out, ensure_ascii=False)
    assert CANARY not in blob and "SANDBOX-VAL-9" not in blob
    assert out["error"] == code and out["error_code"] == code
    assert out["messages"][-1]["content"].endswith(f"（错误码：{code}）")
    assert "draft" not in out and "ask" not in out


def test_guard_exception_fails_closed_with_code_only(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"db exploded {CANARY}")

    monkeypatch.setattr(node_wrap, "guard_node_output", boom)
    out = wrap_node("chat", _leaky_node)({"user_id": "rv-u1", "thread_id": "rv-t1"})
    _assert_fail_closed(out, GUARD_UNAVAILABLE)
    assert out["loop_last_error"] == GUARD_UNAVAILABLE
    row = _rows("rv-t1")[-1]
    assert not row["ok"] and row["error"].startswith(GUARD_UNAVAILABLE)
    assert CANARY not in json.dumps(row, ensure_ascii=False)
    assert any(g.get("type") == "guard_error" for g in row["guardrails"])


def test_profile_lookup_failure_fails_closed(monkeypatch):
    """A DB error while loading data the leak scan needs must not degrade to a partial scan."""

    def broken(uid):
        raise RuntimeError("no such table: user_profiles")

    monkeypatch.setattr(db, "get_profile", broken)
    ran = []
    out = wrap_node("chat", lambda s: ran.append(1) or _leaky_node(s))({"user_id": "rv-u2", "thread_id": "rv-t2"})
    _assert_fail_closed(out, GUARD_UNAVAILABLE)
    assert not ran, "node must not run when its sandbox values are unknown"


def test_other_user_index_failure_fails_closed(monkeypatch):
    def broken():
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(leak, "_foreign_values", lambda text: broken())
    out = wrap_node("chat", _leaky_node)({"user_id": "rv-u3", "thread_id": "rv-t3"})
    _assert_fail_closed(out, GUARD_UNAVAILABLE)
    with pytest.raises(RuntimeError):
        leak.scan_output("x", user_id="u", profile={}, strict=True)
    assert leak.scan_output("x", user_id="u", profile={})["text"] == "x"


@user_scope("rv-u4")
def test_node_exception_text_never_reaches_client():
    def node(state):
        raise ValueError(f"vendor said {CANARY} at /srv/app/secret.py")

    out = wrap_node("draft", node)({"user_id": "rv-u4", "thread_id": "rv-t4"})
    _assert_fail_closed(out, NODE_FAILED)
    assert "/srv/app" not in json.dumps(out, ensure_ascii=False)
    row = _rows("rv-t4")[-1]
    assert "ValueError" in row["error"] and CANARY not in row["error"]


def test_request_sandbox_reaches_exit_guard():
    def node(state):
        return {"messages": [{"role": "assistant", "content": "代号 REQ-SBX-77"}]}

    out = wrap_node("chat", node)(
        {"user_id": "rv-u5", "thread_id": "rv-t5", "profile": {"privacy_sandbox": ["REQ-SBX-77"]}}
    )
    assert "REQ-SBX-77" not in out["messages"][-1]["content"]


# ------------------------------------------------------------------ P0-2 gate


def _golden_runner():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.golden import run_golden

    return run_golden


def _res(i, status, safety, cat="comfort"):
    return {"id": f"c{i}", "category": cat, "safety": safety, "status": status, "failures": []}


def test_gate_live_nine_of_ten_with_safety_fail_is_fail():
    rg = _golden_runner()
    results = [_res(i, "pass", False) for i in range(9)] + [_res(9, "fail", True, "leak")]
    s = rg._summarize(results, "live", 0.9, {"live": {"quality_pass_rate": 0.9}})
    assert s["pass_rate"] == 0.9
    assert s["quality"]["ok"] and not s["safety"]["ok"] and not s["ok"]
    assert s["safety"]["failed_ids"] == ["c9"]


def test_gate_quality_compares_to_baseline():
    rg = _golden_runner()
    results = [_res(i, "pass", False) for i in range(8)] + [_res(8, "fail", False), _res(9, "pass", True, "crisis")]
    assert not rg._summarize(results, "live", None, {"live": {"quality_pass_rate": 0.95}})["ok"]
    assert rg._summarize(results, "live", None, {"live": {"quality_pass_rate": 0.85}})["ok"]


def test_gate_all_safety_skipped_is_not_a_pass():
    rg = _golden_runner()
    results = [_res(0, "pass", False), _res(1, "skipped", True, "leak")]
    assert not rg._summarize(results, "mock", None, {})["safety"]["ok"]


def test_safety_categories_cover_case_files():
    rg = _golden_runner()
    cases = rg._load_cases(str(Path(rg.HERE) / "cases"), "")
    cats = {c["category"] for c in cases if c["safety"]}
    assert {"crisis", "leak"} <= cats


# ------------------------------------------------------------------ P0-3 honest LLM status


class _Resp:
    def __init__(self, content, pt=11, ct=7):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = SimpleNamespace(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct)


class _Client:
    def __init__(self, script):
        self.script = list(script)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def fake_vendor(monkeypatch):
    import app.llm as llm
    from app.gateway import core, llm_gateway, providers
    from app.gateway.config import GatewayConfig, ModelSpec

    def setup(script, model="vendor-fast-1"):
        client = _Client(script)
        monkeypatch.setattr(
            llm,
            "resolve_tier",
            lambda tier: {"has_key": True, "model": model, "provider": "openai", "api_key": "k", "base_url": "u"},
        )
        def config():
            # Resolve dynamically so the escalation test can customize both tiers.
            models = {}
            for tier in llm.ModelTier:
                cfg = llm.resolve_tier(tier)
                models[tier.value] = ModelSpec(
                    id=tier.value, model=cfg["model"], vendor=cfg["provider"],
                    base_url=cfg["base_url"], legacy_tier=tier.value,
                )
            return GatewayConfig(models, {t: [t] for t in models}, {}, [])

        def post(*args, **kwargs):
            resp = client._create()
            return {
                "choices": [{"message": {"content": resp.choices[0].message.content}}],
                "usage": vars(resp.usage),
            }

        monkeypatch.setattr(core, "get_config", config)
        monkeypatch.setattr(llm_gateway, "get_config", config)
        monkeypatch.setattr(providers, "_post", post)
        monkeypatch.setattr(core, "_sleep", lambda s: None)
        monkeypatch.setenv("LLM_RETRY_ON_TIMEOUT", "1")
        monkeypatch.setenv("LLM_MAX_RETRIES", "2")
        monkeypatch.setenv("LLM_EMPTY_ESCALATE", "0")
        return client

    return setup


def _one_call(fn):
    with trace_ctx.step_context(tool="t") as ctx:
        out = fn()
    assert len(ctx["llm_calls"]) == 1
    return out, ctx["llm_calls"][0]


def test_provider_failure_with_fallback_is_not_ok(fake_vendor):
    from app.gateway import llm_gateway as gw

    fake_vendor([TimeoutError(f"upstream {CANARY}")] * 3)
    out, call = _one_call(lambda: gw.chat_json("s", "u", fallback={"a": 1}))
    assert out["a"] == 1
    assert call["ok"] is False
    assert call["fallback_reason"] == "provider_error:TimeoutError"
    assert call["requested_model"] == "vendor-fast-1" and call["actual_model"] == ""
    assert call["attempts"] == 3 and len(call["attempt_errors"]) == 3
    assert call["mode"] == "mock_fallback" and CANARY not in json.dumps(call)
    assert CANARY not in out.get("_model_error", "")

    fake_vendor([RuntimeError("down")] * 3)
    txt, call = _one_call(lambda: gw.chat_text("s", "u", fallback="兜底"))
    assert txt == "兜底" and call["ok"] is False and call["fallback_reason"].startswith("provider_error")


def test_retry_then_success_records_attempts_and_usage(fake_vendor):
    from app.gateway import llm_gateway as gw

    fake_vendor([ConnectionError("x"), _Resp('{"ok": 1}', 20, 5)])
    out, call = _one_call(lambda: gw.chat_json("s", "u", fallback={}))
    assert out["ok"] == 1 and call["ok"] is True and call["fallback_reason"] == ""
    assert call["attempts"] == 2 and call["actual_model"] == "vendor-fast-1"
    assert call["usage"] == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}


def test_parse_failure_and_empty_content_are_flagged(fake_vendor):
    from app.gateway import llm_gateway as gw

    fake_vendor([_Resp("not json")])
    _, call = _one_call(lambda: gw.chat_json("s", "u", fallback={"d": 1}))
    assert call["ok"] is False and call["fallback_reason"] == "json_parse_error"

    fake_vendor([_Resp("")])
    _, call = _one_call(lambda: gw.chat_text("s", "u", fallback="fb"))
    assert call["ok"] is False and call["fallback_reason"] == "empty_content"


def test_empty_content_escalation_records_actual_model(fake_vendor, monkeypatch):
    import app.llm as llm
    from app.gateway import llm_gateway as gw

    client = fake_vendor([_Resp(""), _Resp("strong answer")])
    monkeypatch.setenv("LLM_EMPTY_ESCALATE", "1")
    monkeypatch.setattr(
        llm,
        "resolve_tier",
        lambda tier: {
            "has_key": True,
            "model": "vendor-fast-1" if tier == llm.ModelTier.FAST else "vendor-strong-1",
            "provider": "openai",
            "api_key": "k",
            "base_url": "u",
        },
    )
    txt, call = _one_call(lambda: gw.chat_text("s", "u"))
    assert txt == "strong answer" and call["ok"] is True
    assert call["requested_model"] == "vendor-fast-1" and call["actual_model"] == "vendor-strong-1"
    assert call["model_fallback"].startswith("empty_content") and not client.script


@user_scope("rv-u6")
def test_step_trace_marks_degraded_llm(fake_vendor):
    from app.gateway import llm_gateway as gw

    fake_vendor([RuntimeError("down")] * 3)
    wrap_node("chat", lambda s: {"messages": [{"role": "assistant", "content": gw.chat_text("s", "u", fallback="fb")}]})(
        {"user_id": "rv-u6", "thread_id": "rv-t6"}
    )
    row = _rows("rv-t6")[-1]
    assert row["detail"]["llm_failed"] == 1 and row["detail"]["degraded"] is True
    assert row["llm_calls"][0]["ok"] is False


# ------------------------------------------------------------------ P0-4 async traces


def test_trace_writes_are_async_and_flushed_for_readers(monkeypatch):
    monkeypatch.delenv("TRACE_WRITE_MODE", raising=False)
    slow = []
    real_insert = traces._insert

    def slow_insert(recs):
        slow.append(len(recs))
        return real_insert(recs)

    monkeypatch.setattr(traces, "_insert", slow_insert)
    assert traces.record_step(thread_id="rv-async", user_id="u", tool="x") == {"queued": True}
    assert traces.flush(5.0)
    assert slow and _rows("rv-async")


def test_trace_write_failure_is_counted_not_silent(monkeypatch):
    before = traces.writer_stats()

    def broken_session():
        raise RuntimeError("disk full")

    monkeypatch.setattr(traces, "SessionLocal", broken_session)
    traces.record_step(thread_id="rv-fail", user_id="u", tool="x")
    assert traces.flush(5.0)
    after = traces.writer_stats()
    assert after["write_errors"] > before["write_errors"]
    assert after["rows_lost"] > before["rows_lost"] and after["last_error"] == "RuntimeError"


def test_budget_exhaustion_is_traced():
    ran = []
    st = {"user_id": "rv-u7", "thread_id": "rv-t7", "loop_step": 5, "loop_budget": {"max_steps": 5}, "loop_escalated": True}
    out = wrap_node("chat", lambda s: ran.append(1) or {})(st)
    assert not ran and out["error"] == "loop_budget:max_steps"
    row = _rows("rv-t7")[-1]
    assert not row["ok"] and row["detail"]["budget_stop"] == "max_steps"
    assert row["guardrails"][0]["guardrail"] == "loop_budget"


def test_admin_api_exposes_writer_stats(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("HARNESS_ADMIN_TOKEN", "a" * 32)
    with TestClient(app) as c:
        r = c.get("/api/admin/traces", headers={"X-Admin-Token": "a" * 32})
    data = r.json()["data"]
    assert {"write_errors", "dropped_queue_full", "pending", "mode"} <= set(data["writer"])
    assert data["summary"]["scope"] == "all" and "llm_failed" in data["summary"]
    assert data["page_summary"]["scope"] == "page"


# ------------------------------------------------------------------ P1 redaction / truncation


def test_token_telemetry_is_not_mistaken_for_secret():
    out = redact_obj(
        {
            "tokens_in_est": 123,
            "tokens_out_est": 4,
            "max_tokens": 50,
            "prompt_tokens": 9,
            "access_token": "abc",
            "hookToken": "abc",
            "api_key": "x",
        }
    )
    assert out["tokens_in_est"] == 123 and out["tokens_out_est"] == 4
    assert out["max_tokens"] == 50 and out["prompt_tokens"] == 9
    assert out["access_token"] == out["hookToken"] == out["api_key"] == "***"


def test_numeric_phone_and_id_are_masked():
    out = redact_obj({"phone": 13800138000, "contact": {"mobile": "13800138000"}, "n": 13912345678, "tel": "021-12345678"})
    assert "13800138000" not in json.dumps(out) and "13912345678" not in json.dumps(out)
    assert out["phone"] == "138****8000" and out["n"] == "139****5678"
    assert "12345678" not in out["tel"]


def test_large_llm_call_list_is_truncated_not_emptied():
    calls = [{"kind": "text", "task": "t" * 250, "tier": "fast", "tokens_in_est": i} for i in range(60)]
    traces.record_step(thread_id="rv-trunc", user_id="u", tool="x", llm_calls=calls, sync=True)
    row = _rows("rv-trunc")[-1]
    assert row["llm_calls"], "truncation must keep a list, not drop to []"
    assert row["llm_calls"][0]["tokens_in_est"] == 0
    assert row["detail"]["llm_calls_dropped"] == 60 - len(row["llm_calls"])
    assert row["detail"]["llm_calls_total"] == 60


@pytest.mark.parametrize("loader", ["profile", "index"])
@pytest.mark.parametrize("guard_name", ["guard_text", "guard_outbound_text"])
def test_delivery_loader_failure_withholds(monkeypatch, loader, guard_name):
    from app.agent.harness import guardrails

    def broken(*args):
        raise RuntimeError("private loader failure")

    monkeypatch.setattr(db, "get_profile", broken if loader == "profile" else lambda uid: {})
    if loader == "index":
        monkeypatch.setattr(leak, "_foreign_values", broken)
    with trace_ctx.step_context(tool="delivery") as ctx:
        out = getattr(guardrails, guard_name)("private message", user_id="owner")
    assert out == leak.PROMPT_LEAK_REPLY
    assert ctx["guardrails"][-1]["blocked"] is True


@pytest.mark.parametrize("owner_email", ["owner1@own.example", "OWNER1@OWN.EXAMPLE"])
@pytest.mark.parametrize("indexed", [False, True])
def test_email_ownership_uses_full_address(monkeypatch, owner_email, indexed):
    index = {"owner1@own.example": {"other"}} if indexed else {}
    monkeypatch.setattr(leak, "_foreign_values", lambda text: {v: o for v, o in index.items() if v in text})
    out = leak.scan_output(
        "owner1@own.example victim1@foreign.example",
        user_id="owner", profile={"email": owner_email}, strict=True,
    )
    assert out["text"] == "owner1@own.example " + leak.OTHER_USER_PLACEHOLDER
    assert {"type": "other_user_data", "count": 1} in out["hits"]


def test_underscore_fields_are_scanned(monkeypatch):
    from app.agent.harness.guardrails import guard_node_output

    monkeypatch.setattr(leak, "_foreign_values", lambda text: {})
    out, hits = guard_node_output(
        {"draft": {"_raw": "PRIVATE-CONTENT-77"}}, user_id="owner",
        profile={"privacy_sandbox": ["PRIVATE-CONTENT-77"]},
    )
    assert out["draft"]["_raw"] == leak.SANDBOX_PLACEHOLDER
    assert any(h["where"] == "draft._raw" for h in hits)


@pytest.mark.parametrize("failure", ["start", "queue"])
def test_async_trace_submission_failure_never_raises(monkeypatch, failure):
    writer = traces._Writer()
    monkeypatch.setattr(traces, "_writer", writer)

    def broken(*args):
        raise RuntimeError("submission unavailable")

    if failure == "start":
        monkeypatch.setattr(traces.threading.Thread, "start", broken)
    else:
        monkeypatch.setattr(writer, "_ensure_thread", lambda: None)
        monkeypatch.setattr(writer.q, "put_nowait", broken)
    before = traces.writer_stats()["write_errors"]
    assert traces.record_step(thread_id="submission-failure", sync=False) == {}
    assert writer.pending() == 0
    assert traces.writer_stats()["write_errors"] == before + 1


@pytest.mark.parametrize("content,error", [("", "empty_output"), ("not json", "bad_json")])
def test_json_failure_has_error_code(fake_vendor, content, error):
    from app.gateway import llm_gateway as gw

    fake_vendor([_Resp(content)])
    out, call = _one_call(lambda: gw.chat_json("s", "u", fallback={"fallback": True}))
    assert out["fallback"] is True
    assert call["ok"] is False
    assert call["error"] == error


def test_empty_text_has_error_code(fake_vendor):
    from app.gateway import llm_gateway as gw

    fake_vendor([_Resp("")])
    out, call = _one_call(lambda: gw.chat_text("s", "u", fallback="fallback"))
    assert out == "fallback"
    assert call["ok"] is False
    assert call["error"] == "empty_output"
