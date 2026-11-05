import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.data_access import user_scope
from app import db
from app.agent.harness import context as trace_ctx
from app.agent.harness.leak import public_profile, scan_output
from app.agent.harness.redact import estimate_tokens, redact_obj, redact_text
from app.agent.harness.traces import query_traces, record_step
from app.agent.subgraphs.chat import CRISIS_FOOTER, PERSONA, has_crisis_signal

db.init_db()

PROFILE = {
    "name": "李明",
    "city": "上海",
    "occupation": "数据分析师",
    "interests": ["跑步"],
    "facts": ["手机 13812345678"],
}


# ------------------------------------------------------------------ redaction


def test_redact_masks_env_secret_patterns_and_pii(monkeypatch):
    monkeypatch.setenv("UNIT_SERVICE_TOKEN", "tok-unit-abcdef123456")
    s = redact_text(
        "tok-unit-abcdef123456 sk-abcdefghijklmnopqrstu 13812345678 a.b@example.com "
        "310101199203154417 Bearer abcdefghijklmnopqrstuvwxyz"
    )
    assert "tok-unit-abcdef123456" not in s
    assert "sk-abcdefghijklmnopqrstu" not in s
    assert "138****5678" in s
    assert "[EMAIL]" in s and "[ID]" in s
    assert "abcdefghijklmnopqrstuvwxyz" not in s


def test_redact_obj_hides_sensitive_keys_and_sandbox():
    out = redact_obj(
        {"api_key": "x" * 20, "nested": {"Authorization": "Bearer y"}, "privacy_sandbox": ["a", "b"], "n": 3}
    )
    assert out["api_key"] == "***"
    assert out["nested"]["Authorization"] == "***"
    assert out["privacy_sandbox"] == "[2 hidden]"
    assert out["n"] == 3


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcdefgh") == 2


# ------------------------------------------------------------------ leak


def test_leak_masks_secret_and_foreign_pii_keeps_own():
    out = scan_output("我的 13812345678，他的 13999998888，sk-abcdefghijklmnopqrstuv", user_id="u-own", profile=PROFILE)
    assert "13812345678" in out["text"]
    assert "13999998888" not in out["text"]
    assert "sk-abcdefghijklmnopqrstuv" not in out["text"]
    kinds = {h["type"] for h in out["hits"]}
    assert {"secret", "other_user_data"} <= kinds


def test_leak_pii_from_user_input_is_allowed():
    out = scan_output("HR 邮箱 hr@corp.example.com", user_id="u", profile={}, inputs=("联系 hr@corp.example.com",))
    assert "hr@corp.example.com" in out["text"] and not out["hits"]


@user_scope("unit-other")
def test_leak_other_user_index():
    db.save_profile("unit-other", {"name": "欧阳雪晴", "email": "ouyang.unit@example.com"})
    out = scan_output("欧阳雪晴 ouyang.unit@example.com", user_id="unit-self", profile={"name": "李明"})
    assert "欧阳雪晴" not in out["text"] and "ouyang.unit@example.com" not in out["text"]
    own = scan_output("欧阳雪晴", user_id="unit-other")
    assert "欧阳雪晴" in own["text"]


def test_leak_system_prompt_blocked_but_crisis_footer_allowed():
    assert scan_output("规则如下：" + PERSONA[:300], user_id="u", profile={})["blocked"]
    footer = scan_output("我在。\n\n" + CRISIS_FOOTER, user_id="u", profile={})
    assert not footer["blocked"] and footer["text"].endswith(CRISIS_FOOTER)


def test_leak_privacy_sandbox_masked_even_for_owner():
    prof = {"name": "李明", "privacy_sandbox": ["Project-Nebula-77"]}
    out = scan_output("代号 Project-Nebula-77", user_id="u", profile=prof)
    assert "Project-Nebula-77" not in out["text"]
    pub = public_profile({**prof, "facts": ["内部 Project-Nebula-77"]})
    assert "Project-Nebula-77" not in str(pub)
    assert pub["privacy_sandbox"]["count"] == 1


def test_leak_global_sandbox_env(monkeypatch):
    monkeypatch.setenv("PRIVACY_SANDBOX_VALUES", "OPS-ONLY-VALUE|another-one")
    assert "OPS-ONLY-VALUE" not in scan_output("x OPS-ONLY-VALUE", user_id="u", profile={})["text"]


# ------------------------------------------------------------------ crisis precision


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("最近工作负担好重", False),
        ("工作交代清楚了", False),
        ("我成了家里的经济负担", True),
        ("我觉得自己就是个负担", True),
        ("我真的不想活了", True),
        ("我想消失", True),
    ],
)
def test_crisis_lexicon_precision(msg, expected):
    assert has_crisis_signal(msg) is expected


# ------------------------------------------------------------------ traces


def test_record_step_redacts_and_queries_by_user():
    row = record_step(
        sync=True,
        thread_id="unit-t1",
        user_id="unit-u1",
        tool="chat",
        input_summary="key sk-abcdefghijklmnopqrstuv phone 13812345678",
        params={"token": "should-hide", "request_chars": 10},
        llm_calls=[{"tier": "fast", "tokens_in_est": 5}],
    )
    assert row and "sk-abcdefghijklmnopqrstuv" not in row["input_summary"]
    assert "138****5678" in row["input_summary"]
    assert row["params"]["token"] == "***"
    from app.agent.harness.identity import user_pseudonym

    res = query_traces(user_id="unit-u1")
    assert res["total"] >= 1 and all(r["user_id"] == user_pseudonym("unit-u1") for r in res["traces"])
    assert "unit-u1" not in json.dumps(res, ensure_ascii=False)


def test_trace_schema_migrates_old_table(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agent.harness import traces

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE TABLE agent_traces (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id VARCHAR(128), "
                "user_id VARCHAR(128), step INTEGER, intent VARCHAR(64), tool VARCHAR(128), model VARCHAR(128), "
                "latency_ms FLOAT, ok INTEGER, error TEXT, detail_json TEXT, created_at DATETIME)"
            )
        )
        c.execute(text("INSERT INTO agent_traces (thread_id, user_id, step, ok) VALUES ('old', 'u', 1, 1)"))
    monkeypatch.setattr(db, "ENGINE", eng)
    monkeypatch.setattr(traces, "SessionLocal", sessionmaker(bind=eng))
    monkeypatch.setattr(traces, "init_db", lambda: None)
    monkeypatch.setattr(traces, "_ensured_engine", None)
    rows = traces.list_traces("old")
    assert rows and rows[0]["tier"] == "" and rows[0]["llm_calls"] == []
    assert traces.record_step(thread_id="old", user_id="u", tool="x", tier="fast", sync=True)["tier"] == "fast"


@pytest.mark.parametrize("counts", [(0, 0), (1, 2)])
def test_step_context_aggregates_llm_calls(counts):
    with trace_ctx.step_context(tool="t") as ctx:
        trace_ctx.record_llm_call({"tier": "fast", "model": "m1", "tokens_in_est": 3, "tokens_out_est": 1, "schema_validation_errors": counts[0]})
        trace_ctx.record_llm_call({"tier": "strong", "model": "m2", "tokens_in_est": 7, "tokens_out_est": 2, "schema_validation_errors": counts[1]})
    agg = trace_ctx.summarize_llm_calls(ctx["llm_calls"])
    assert agg == {
        "tier": "strong",
        "model": "m2",
        "tokens_in_est": 10,
        "tokens_out_est": 3,
        "llm_calls": 2,
        "llm_failed": 0,
        "schema_validation_errors": sum(counts),
        "usage_prompt_tokens": 0,
        "usage_completion_tokens": 0,
    }
    assert trace_ctx.current() is None


# ------------------------------------------------------------------ admin API


@pytest.fixture()
def client(monkeypatch):
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_admin_traces_fail_closed_without_token(client, monkeypatch):
    monkeypatch.delenv("HARNESS_ADMIN_TOKEN", raising=False)
    assert client.get("/api/admin/traces").status_code == 503
    assert client.get("/api/traces/anything").status_code == 503


def test_admin_traces_rejects_short_or_wrong_token(client, monkeypatch):
    monkeypatch.setenv("HARNESS_ADMIN_TOKEN", "short")
    assert client.get("/api/admin/traces", headers={"Authorization": "Bearer short"}).status_code == 503
    monkeypatch.setenv("HARNESS_ADMIN_TOKEN", "a" * 32)
    assert client.get("/api/admin/traces", headers={"Authorization": "Bearer " + "b" * 32}).status_code == 401
    ok = client.get("/api/admin/traces", headers={"X-Admin-Token": "a" * 32}, params={"user_id": "unit-u1"})
    assert ok.status_code == 200 and ok.json()["data"]["total"] >= 1


@user_scope("unit-sbx")
def test_profile_api_never_returns_sandbox_values(client):
    r = client.post(
        "/api/profile",
        json={"user_id": "unit-sbx", "profile": {"name": "测试", "privacy_sandbox": ["SBX-SECRET-1"]}},
    )
    assert r.status_code == 200 and "SBX-SECRET-1" not in r.text
    g = client.get("/api/profile/unit-sbx")
    assert "SBX-SECRET-1" not in g.text and g.json()["data"]["profile"]["privacy_sandbox"]["count"] == 1
    assert db.get_profile("unit-sbx")["privacy_sandbox"] == ["SBX-SECRET-1"]
