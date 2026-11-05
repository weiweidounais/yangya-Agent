from app.agent.harness import traces
from app.agent.node_wrap import _input_summary

SENTINEL = "QA-SENTINEL-pw-7f3a91c0d2"
RAW = f"我的密码是 {SENTINEL} 帮我登录"


def test_guard_masks_when_kinds_present():
    out = traces._sensitive_guard(RAW, ["password"])
    assert SENTINEL not in out and "password" in out and out.startswith("[redacted:")


def test_guard_detects_without_kinds():
    out = traces._sensitive_guard(RAW, [])
    assert SENTINEL not in out


def test_guard_keeps_benign_text():
    assert traces._sensitive_guard("帮我看看这个JD", []) == "帮我看看这个JD"


def test_privacy_gate_row_never_stores_raw():
    row = traces._build_row({"thread_id": "t1", "user_id": "u1", "intent": "privacy_gate",
                             "tool": "privacy_gate", "input_summary": RAW,
                             "detail": {"kinds": ["password"]}})
    assert SENTINEL not in row.input_summary
    assert SENTINEL not in row.detail_json
    assert "password" in row.input_summary


def test_node_wrap_input_summary_masked():
    out = _input_summary({"action": "chat", "message": RAW}, [])
    assert SENTINEL not in out


def test_chat_api_privacy_gate_trace(client):
    import json

    r = client.post("/api/chat", json={"user_id": "trace-sentinel", "message": f"boss 的密码是 {SENTINEL}"})
    assert r.status_code == 200
    rows = traces.list_user_traces("trace-sentinel", limit=50)
    gate = [x for x in rows if x.get("intent") == "privacy_gate"]
    assert gate, rows
    assert SENTINEL not in json.dumps(rows, ensure_ascii=False)
    assert gate[0]["input_summary"].startswith("[redacted:")
