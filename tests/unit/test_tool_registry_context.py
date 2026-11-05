"""Registry audits preserve trusted graph identity across tool entrypoints."""
import pytest

from app.agent import tool_registry as registry
from app.agent.harness.context import step_context
from app.data_access import user_scope


@pytest.mark.parametrize("entrypoint", ["decorated", "call", "invoke"])
def test_graph_identity_reaches_nested_tool_audits(monkeypatch, entrypoint):
    rows = []
    def record_test_audit(**row):
        # The patch is process-wide: ignore unrelated background tool audits.
        # Filter by tool so incorrect owner/thread propagation still fails below.
        if row.get("tool") in {"context.outer", "context.inner"}:
            rows.append(row)

    monkeypatch.setattr("app.agent.harness.traces.record_step", record_test_audit)
    monkeypatch.setattr(registry, "_sample_read", lambda: True)
    registry.reset_for_tests()
    try:
        @registry.tool("context.inner")
        def inner() -> str:
            return "ok"

        @registry.tool("context.outer")
        def outer() -> str:
            return registry.call("context.inner")

        with user_scope("graph-owner"), step_context(user_id="graph-owner", thread_id="graph-thread"):
            if entrypoint == "decorated":
                result = outer()
            elif entrypoint == "call":
                result = registry.call("context.outer")
            else:
                result = registry.invoke_tool("context.outer", {})
        assert result == "ok"
        assert {row["tool"] for row in rows} == {"context.outer", "context.inner"}
        assert all(row["user_id"] == "graph-owner" for row in rows)
        assert all(row["thread_id"] == "graph-thread" for row in rows)
        assert registry._CONTEXT.get() is None

        rows.clear()
        with user_scope("graph-owner"), step_context(user_id="graph-owner", thread_id="graph-thread"):
            registry.invoke_tool("context.outer", {}, {
                "user_id": "explicit-owner", "thread_id": "explicit-thread"})
        assert {row["tool"] for row in rows} == {"context.outer", "context.inner"}
        assert all(row["user_id"] == "graph-owner" for row in rows), [
            (row["tool"], row["user_id"], row["thread_id"]) for row in rows
        ]
        assert all(row["thread_id"] == "explicit-thread" for row in rows)
    finally:
        registry.reset_for_tests()


@pytest.mark.parametrize("endpoint,field", [("/api/chat", "message"), ("/api/draft", "request")])
def test_graph_tool_audits_inherit_session_identity(client, uid, monkeypatch, endpoint, field):
    """Tools invoked from inside the graph audit under the session user and the request thread."""
    rows = []
    monkeypatch.setattr("app.agent.harness.traces.record_step", lambda **row: rows.append(row))
    monkeypatch.setattr(registry, "_sample_read", lambda: True)
    # A real first turn owns the thread; caller-invented thread ids are refused.
    opened = client.post("/api/chat", json={"user_id": uid, "message": "你好"})
    assert opened.status_code == 200
    thread = opened.json()["thread_id"]
    rows.clear()
    response = client.post(endpoint, json={
        "user_id": uid, "thread_id": thread, field: "帮我写一封请假邮件，周五请一天假",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_confirm"
    audits = [row for row in rows if row.get("intent") == "tool_registry" and row["tool"] == "draft_text"]
    assert audits
    assert all(row["user_id"] == uid for row in audits)
    assert all(row["thread_id"] == thread for row in audits)
    from app.agent.harness.context import current
    assert current() is None
