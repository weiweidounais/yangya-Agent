"""Trace store P2: pseudonymous ids, trace/span ids, full-set summaries."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db
from app.agent.harness import traces
from app.agent.harness.identity import thread_pseudonym, user_pseudonym


def _seed(tid: str, uid: str, n: int) -> None:
    for i in range(n):
        traces.record_step(
            sync=True,
            thread_id=tid,
            user_id=uid,
            step=i,
            tool="chat",
            latency_ms=10.0,
            tokens_in_est=3,
            ok=i % 5 != 0,
            llm_calls=[{"ok": i % 2 == 0}, {"ok": True}],
            guardrails=[{"type": "x"}] if i % 4 == 0 else [],
            trace_id=f"{i:032x}" if i == 7 else "",
        )


def test_ids_pseudonymised_and_queryable_by_raw_or_hashed():
    _seed("p2-thread", "p2-user", 3)
    by_raw = traces.query_traces(thread_id="p2-thread")
    by_hash = traces.query_traces(thread_id=thread_pseudonym("p2-thread"))
    assert by_raw["total"] == by_hash["total"] == 3
    assert {r["user_id"] for r in by_raw["traces"]} == {user_pseudonym("p2-user")}
    with db.SessionLocal() as s:
        raw = s.execute(text("SELECT count(*) FROM agent_traces WHERE user_id = 'p2-user' OR thread_id = 'p2-thread'")).scalar()
    assert raw == 0


def test_legacy_raw_rows_still_match():
    traces._ensure()
    with db.SessionLocal() as s:
        s.add(traces.TraceStep(thread_id="legacy-t", user_id="legacy-u", step=1, ok=1, tool="old"))
        s.commit()
    assert traces.query_traces(user_id="legacy-u")["total"] == 1
    assert traces.query_traces(thread_id="legacy-t")["total"] == 1


def test_trace_id_column_and_filter():
    _seed("p2-tid", "p2-tid-user", 8)
    res = traces.query_traces(trace_id=f"{7:032x}")
    assert res["total"] == 1 and res["traces"][0]["step"] == 7


def test_summary_covers_full_set_not_page():
    _seed("p2-sum", "p2-sum-user", 20)
    res = traces.query_traces(thread_id="p2-sum", limit=5, with_summary=True)
    s = res["summary"]
    assert len(res["traces"]) == 5 and res["total"] == 20
    assert s["scope"] == "all" and s["steps"] == 20
    assert s["errors"] == 4  # i % 5 == 0
    assert s["llm_calls"] == 40 and s["llm_failed"] == 10
    assert s["guardrail_hits"] == 5
    assert s["tokens_in_est"] == 60 and s["latency_ms"] == 200.0
    page = traces.page_summary(res["traces"])
    assert page["scope"] == "page" and page["steps"] == 5


def test_admin_api_reports_both_summaries(monkeypatch):
    from app.main import app

    _seed("p2-api", "p2-api-user", 12)
    monkeypatch.setenv("HARNESS_ADMIN_TOKEN", "b" * 32)
    with TestClient(app) as c:
        r = c.get("/api/admin/traces", params={"thread_id": "p2-api", "limit": 3}, headers={"X-Admin-Token": "b" * 32})
    data = r.json()["data"]
    assert data["summary"]["steps"] == 12 and data["page_summary"]["steps"] == 3
    assert "p2-api-user" not in json.dumps(data["traces"], ensure_ascii=False)
