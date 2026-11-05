"""Langfuse / OTel export: hierarchy, redaction before export, never breaks requests,
REST client (MockTransport), user feedback → score → dataset."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.data_access import user_scope
from app.gateway.llm_gateway import set_llm_override
from app.main import app
from app.observability import cost, langfuse_client, tracing
from app.observability.config import langfuse_config
from app.observability.langfuse_client import LangfuseClient

REPLY = "听起来这周真的很累，想聊聊是哪件事最让你难受吗？"


def _mock_llm(kind, system, user, tier, fallback):
    return REPLY if kind == "text" else dict(fallback or {})


@pytest.fixture()
def spans():
    exp = InMemorySpanExporter()
    tracing.use_test_exporter(exp)
    set_llm_override(_mock_llm)
    try:
        yield exp
    finally:
        set_llm_override(None)
        tracing.clear_test_exporter()


def _chat(c, uid, msg, thread_id=None):
    return c.post("/api/chat", json={"user_id": uid, "message": msg, "thread_id": thread_id})


def _tree(exp):
    ss = exp.get_finished_spans()
    by = {s.context.span_id: s for s in ss}
    return ss, {s.name: (by[s.parent.span_id].name if s.parent and s.parent.span_id in by else None) for s in ss}


def test_request_node_generation_hierarchy(spans):
    with TestClient(app) as c:
        r = _chat(c, "obs-h1", "最近工作好累")
    tid = r.headers["x-trace-id"]
    ss, parent = _tree(spans)
    assert parent["node.chat"] == "POST /api/chat"
    assert any(n.startswith("llm.") and p == "node.chat" for n, p in parent.items())
    assert {format(s.context.trace_id, "032x") for s in ss} == {tid}
    types = {s.name: s.attributes.get("langfuse.observation.type") for s in ss}
    assert types["node.chat"] == "agent" and types["POST /api/chat"] == "span"
    assert any(t == "generation" for t in types.values())
    from app.agent.harness.identity import thread_pseudonym, user_pseudonym

    node = next(s for s in ss if s.name == "node.chat")
    assert node.attributes["langfuse.user.id"] == user_pseudonym("obs-h1")
    assert node.attributes["langfuse.session.id"] == thread_pseudonym(r.json()["thread_id"])


def test_generation_records_models_usage_cost_prompt_version(spans, monkeypatch):
    monkeypatch.setenv("LLM_PRICES_JSON", json.dumps({"*": {"input": 1.0, "output": 2.0}}))
    with TestClient(app) as c:
        _chat(c, "obs-g1", "今天被拒了")
    gens = [s for s in spans.get_finished_spans() if s.attributes.get("langfuse.observation.type") == "generation"]
    assert gens
    a = gens[-1].attributes
    assert a["gen_ai.request.model"].startswith("override:") and a["gen_ai.response.model"]
    assert a["gen_ai.usage.input_tokens"] > 0 and a["gen_ai.usage.output_tokens"] > 0
    assert a["langfuse.observation.metadata.usage_source"] == "estimated"
    named = [g for g in gens if g.attributes.get("langfuse.observation.metadata.prompt_name")]
    assert named and len(named[0].attributes["langfuse.observation.metadata.prompt_version"]) == 12
    assert named[0].attributes["langfuse.observation.metadata.prompt_file_version"] == "v1"
    # override / mock calls are never billed
    assert "langfuse.observation.cost_details" not in a


@user_scope("obs-r1")
def test_secrets_pii_sandbox_and_raw_ids_never_exported(spans, monkeypatch):
    monkeypatch.setenv("UNIT_VENDOR_API_KEY", "vendor-secret-0123456789abcdef")
    from app import db

    db.save_profile("obs-r1", {"name": "李明", "privacy_sandbox": ["Nebula-Code-7781"]})
    with TestClient(app) as c:
        _chat(c, "obs-r1", "电话 13812345678 邮箱 li.ming@example.com 代号 Nebula-Code-7781 key vendor-secret-0123456789abcdef")
    blob = json.dumps(
        [{**dict(s.attributes), "events": [dict(e.attributes) for e in s.events]} for s in spans.get_finished_spans()],
        ensure_ascii=False,
    )
    for raw in ("13812345678", "li.ming@example.com", "Nebula-Code-7781", "vendor-secret-0123456789abcdef", "obs-r1"):
        assert raw not in blob, raw


def test_export_io_none_keeps_text_out(monkeypatch):
    exp = InMemorySpanExporter()
    tracing.use_test_exporter(exp, replace(langfuse_config(), enabled=True, export_io="none"))
    set_llm_override(_mock_llm)
    try:
        with TestClient(app) as c:
            _chat(c, "obs-io", "我好焦虑")
        attrs = [dict(s.attributes) for s in exp.get_finished_spans()]
        assert attrs and not any("langfuse.observation.input" in a or "langfuse.observation.output" in a for a in attrs)
    finally:
        set_llm_override(None)
        tracing.clear_test_exporter()


def test_redacting_exporter_scrubs_raw_span_attributes():
    exp = InMemorySpanExporter()
    tracing.use_test_exporter(exp)
    try:
        with tracing.observe("raw") as obs:
            obs.span.set_attribute("custom", "call 13912345678 or sk-abcdefghijklmnopqrstuvwx")
            obs.span.add_event("e", {"x": "mail a.b@example.com"})
        s = exp.get_finished_spans()[0]
        assert "13912345678" not in s.attributes["custom"] and "sk-abc" not in s.attributes["custom"]
        assert "a.b@example.com" not in s.events[0].attributes["x"]
    finally:
        tracing.clear_test_exporter()


class _Exploding(SpanExporter):
    def export(self, spans):
        raise RuntimeError("collector down")

    def shutdown(self):
        pass


def test_exporter_failure_never_breaks_chat():
    tracing.use_test_exporter(_Exploding())
    set_llm_override(_mock_llm)
    try:
        with TestClient(app) as c:
            r = _chat(c, "obs-x", "你好")
        assert r.status_code == 200 and r.json()["ok"]
    finally:
        set_llm_override(None)
        tracing.clear_test_exporter()


def test_tracer_errors_never_break_chat(monkeypatch):
    def boom():
        raise RuntimeError("otel broken")

    monkeypatch.setattr(tracing, "_tracer", boom)
    set_llm_override(_mock_llm)
    try:
        with TestClient(app) as c:
            r = _chat(c, "obs-y", "你好")
        assert r.status_code == 200 and r.headers.get("x-trace-id")
    finally:
        set_llm_override(None)


def test_disabled_by_default_still_returns_local_trace_id():
    assert not langfuse_config().enabled and not tracing.enabled()
    set_llm_override(_mock_llm)
    try:
        with TestClient(app) as c:
            r = _chat(c, "obs-off", "你好")
        tid = r.headers["x-trace-id"]
        from app.agent.harness.traces import query_traces

        assert len(tid) == 32 and query_traces(trace_id=tid)["total"] >= 1
    finally:
        set_llm_override(None)


def test_config_requires_host_and_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "1")
    assert not langfuse_config().enabled and "HOST" in langfuse_config().reason
    monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com/")
    assert "KEY" in langfuse_config().reason
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-1")
    cfg = langfuse_config()
    assert cfg.enabled and cfg.otlp_endpoint == "https://lf.example.com/api/public/otel/v1/traces"


def test_cost_table_exact_prefix_and_default(monkeypatch):
    monkeypatch.setenv(
        "LLM_PRICES_JSON",
        json.dumps({"deepseek-chat": {"input": 0.27, "output": 1.1}, "moonshot-*": {"input": 2, "output": 2}}),
    )
    assert cost.cost_for("deepseek-chat", 1_000_000, 1_000_000)["total"] == pytest.approx(1.37)
    assert cost.cost_for("moonshot-v1-8k", 500_000, 0)["total"] == pytest.approx(1.0)
    assert cost.cost_for("unknown", 10, 10) is None


# ------------------------------------------------------------------ REST client


def _cfg_on():
    return replace(langfuse_config(), enabled=True, host="https://lf.test", public_key="pk", secret_key="sk", reason="")


def test_client_posts_scores_datasets_runs_prompts():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content or b"{}"), request.headers.get("authorization")))
        return httpx.Response(200, json={"id": "x", "version": 3})

    c = LangfuseClient(_cfg_on(), transport=httpx.MockTransport(handler))
    c.create_score(trace_id="t" * 32, name="user_feedback", value=1.0, comment="好")
    c.create_dataset("golden")
    c.create_dataset_item(dataset_name="golden", input={"a": 1}, expected_output={"b": 2}, item_id="i1", source_trace_id="t" * 32)
    c.create_dataset_run_item(run_name="r1", dataset_item_id="i1", trace_id="t" * 32)
    c.create_prompt(name="chat.persona", prompt="x", labels=["sha-abc"])
    paths = [p for _, p, _, _ in seen]
    assert paths == [
        "/api/public/scores",
        "/api/public/v2/datasets",
        "/api/public/dataset-items",
        "/api/public/dataset-run-items",
        "/api/public/v2/prompts",
    ]
    assert seen[0][2]["traceId"] == "t" * 32 and seen[0][2]["dataType"] == "NUMERIC"
    assert seen[2][2]["sourceTraceId"] == "t" * 32 and seen[2][2]["expectedOutput"] == {"b": 2}
    assert all(a and a.startswith("Basic ") for *_, a in seen)


def test_client_disabled_raises_and_submit_is_noop():
    c = LangfuseClient(replace(langfuse_config(), enabled=False, reason="off"))
    with pytest.raises(langfuse_client.LangfuseError):
        c.create_score(trace_id="t", name="x", value=1)
    assert langfuse_client.submit(lambda cl, **kw: None) is False


# ------------------------------------------------------------------ feedback


def test_feedback_score_and_dataset_flow(monkeypatch):
    calls = []

    def fake_submit(fn, **kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(langfuse_client, "submit", fake_submit)
    set_llm_override(_mock_llm)
    monkeypatch.setenv("HARNESS_ADMIN_TOKENS", "ops:" + "O" * 24)
    try:
        with TestClient(app) as c:
            r = _chat(c, "obs-fb", "被拒了好难过")
            tid, thread = r.headers["x-trace-id"], r.json()["thread_id"]
            bad = c.post("/api/feedback", json={"user_id": "someone-else", "trace_id": tid, "value": "up"})
            assert bad.status_code == 404
            assert c.post("/api/feedback", json={"user_id": "obs-fb", "trace_id": tid, "value": "meh"}).status_code == 400
            ok = c.post(
                "/api/feedback",
                json={"user_id": "obs-fb", "trace_id": tid, "thread_id": thread, "value": "down", "comment": "太长了 13812345678"},
            )
            assert ok.status_code == 200 and ok.json()["data"]["langfuse"] == "queued"
            assert calls and calls[0]["trace_id"] == tid and calls[0]["value"] == -1.0
            assert "13812345678" not in calls[0]["comment"]
            fid = ok.json()["data"]["id"]
            h = {"X-Admin-Token": "O" * 24}
            lst = c.get("/api/admin/feedback", headers=h).json()["data"]
            assert lst["summary"]["down"] >= 1
            res = c.post(f"/api/admin/feedback/{fid}/to-dataset", headers=h, json={"note": "too long"})
            assert res.status_code == 200 and res.json()["data"]["stored"] == "local"
            assert c.post("/api/admin/feedback/999999/to-dataset", headers=h).status_code == 404
    finally:
        set_llm_override(None)


def test_feedback_to_langfuse_dataset_uses_source_trace(monkeypatch):
    from app.observability import feedback as fb_mod

    monkeypatch.setattr(langfuse_client, "submit", lambda fn, **kw: True)
    set_llm_override(_mock_llm)
    try:
        with TestClient(app) as c:
            r = _chat(c, "obs-fb2", "最近好累")
        row = fb_mod.record_feedback(user_id="obs-fb2", trace_id=r.headers["x-trace-id"], value="up")
    finally:
        set_llm_override(None)
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"id": "ok"})

    client = LangfuseClient(_cfg_on(), transport=httpx.MockTransport(handler))
    out = fb_mod.add_to_dataset(row["id"], dataset="regressions", client=client)
    assert out["stored"] == "langfuse"
    item = [b for p, b in seen if p == "/api/public/dataset-items"][0]
    assert item["datasetName"] == "regressions" and item["sourceTraceId"] == row["trace_id"]
    assert item["input"]["steps"] and "obs-fb2" not in json.dumps(item, ensure_ascii=False)
