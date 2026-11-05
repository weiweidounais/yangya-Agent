"""Registry contract, enforcement, legacy entrypoints and owner-only notifications."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from app.agent import tool_registry as registry

SCHEDULE_TOOLS = {
    'schedule.create', 'schedule.list', 'schedule.update', 'schedule.delete',
    'schedule.pause', 'schedule.resume', 'schedule.run_now',
}
CRAWL_TOOLS = {"web_fetch", "web_search", "fetch_feed", "get_hn", "search_social"}
MEMORY_TOOLS = {
    "memory_card", "memory_context", "memory_delete", "memory_delete_all", "memory_episode_delete",
    "memory_episode_update", "memory_fact_delete", "memory_fact_update", "memory_forget", "memory_get",
    "memory_search", "memory_update", "memory_write",
}
PROFILE_TOOLS = {"profile_read", "profile_save", "profile_update"}
# memory_facts_search appears only when the vector flag is on.
TOOL_COUNT = 46
DRAFT_REQUEST = "帮我写一封请假邮件，周五请一天假"


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset_for_tests()
    yield
    registry.reset_for_tests()


def error(out):
    assert out["ok"] is False
    return out["error"]["code"]


@pytest.mark.parametrize("entrypoint", ["invoke", "invoke_raise", "call"])
def test_unknown_tool_refused(entrypoint, caplog):
    name = "does_not_exist"
    with caplog.at_level(logging.WARNING, logger=registry.__name__):
        if entrypoint == "invoke":
            result = registry.invoke_tool(name, {}, {})
        else:
            with pytest.raises(registry.ToolError) as exc:
                if entrypoint == "call":
                    registry.call(name)
                else:
                    registry.invoke_tool(name, {}, {"raise_errors": True})
            result = exc.value.as_dict()
            assert str(exc.value) == "这个操作暂不支持"
    assert result == {"ok": False, "error": {
        "code": "unknown_tool", "message": "这个操作暂不支持"}}
    warnings = [r for r in caplog.records if r.name == registry.__name__]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert name in warnings[0].getMessage()


@pytest.mark.parametrize("vector_enabled", [False, True])
def test_contract_catalog(monkeypatch, vector_enabled, uid):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1" if vector_enabled else "0")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    tools = {s["name"]: s for s in registry.list_tools()}
    assert PROFILE_TOOLS | MEMORY_TOOLS | SCHEDULE_TOOLS | CRAWL_TOOLS | {
        "draft_text", "summarize_turn", "proactive.deliver"} <= tools.keys()
    assert ("memory_facts_search" in tools) == vector_enabled
    assert len(tools) == TOOL_COUNT + int(vector_enabled)
    for item in tools.values():
        assert item["when_to_use"] and item["when_not_to_use"]
        assert item["timeout"] > 0 and item["max_retries"] >= 0
        assert item["status"] in {"live", "dry_run", "not_connected"}
        Draft202012Validator.check_schema(item["params_schema"])
        Draft202012Validator.check_schema(item["return_schema"])
        if item["name"].startswith("proactive."):
            assert item["risk_level"] == "notify_self"
        if item["risk_level"] == "external":
            assert item["requires_confirm"]
    json.dumps(list(tools.values()))


@pytest.mark.parametrize("flag", [None, "0", "false"])
def test_flag_off_catalog_and_schemas_equal_main(monkeypatch, flag):
    from app.agent import tool_selection

    if flag is None:
        monkeypatch.delenv("MEMORY_VECTOR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", flag)
    baseline = json.loads((Path(__file__).parent / "fixtures/main_tool_catalog.json").read_text())
    catalog = registry.list_tools()
    assert [item["name"] for item in catalog] == baseline["names"]
    assert len(baseline["names"]) == TOOL_COUNT

    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    # Frozen from the generic-agent registry; covers every input/output schema
    # and all model-visible metadata, not just tool names or schema presence.
    # Regenerate the fixture deliberately when a contract changes.
    assert digest(catalog) == baseline["catalog_sha256"]
    model_catalog = [item for item in catalog if item["name"] in tool_selection.TOOL_ROUTES]
    assert [item["name"] for item in model_catalog] == sorted(
        name for name in tool_selection.TOOL_ROUTES if name != "memory_facts_search")
    assert digest(model_catalog) == baseline["model_catalog_sha256"]


def test_schema_validation_refuses_before_execution():
    fn = Mock()
    registry.register("strict", fn, params_schema={"type": "object", "properties": {
        "count": {"type": "integer", "minimum": 1}}, "required": ["count"], "additionalProperties": False})
    for args in ({}, {"count": "1"}, {"count": 0}, {"count": True}, {"count": 1, "extra": True}, []):
        assert error(registry.invoke_tool("strict", args)) == "schema_validation"
    fn.assert_not_called()


def test_not_connected_honest_and_no_execution():
    fn = Mock()
    registry.register("offline", fn, status="not_connected")
    result = registry.invoke_tool("offline", {})
    assert error(result) == "not_connected"
    assert "未执行" in result["error"]["message"]
    fn.assert_not_called()


@pytest.mark.parametrize("requires_confirm", [False, True])
def test_external_without_confirm_refused(requires_confirm, uid):
    fn = Mock()
    registry.register("send", fn, risk_level="external", requires_confirm=requires_confirm)
    assert registry.get("send").requires_confirm is True
    registry.get("send").requires_confirm = False
    for ctx in ({"user_id": uid}, {"user_id": uid, "confirmed": True},
                {"user_id": uid, "confirm_token": "forged"}):
        assert error(registry.invoke_tool("send", {}, ctx)).startswith("confirm_")
    fn.assert_not_called()


@pytest.mark.parametrize("risk_level", ["read", "self_write", "notify_self"])
def test_non_external_requires_confirm_rejected(risk_level):
    with pytest.raises(ValueError, match="requires_confirm.*external"):
        registry.register("invalid_confirm", Mock(), risk_level=risk_level, requires_confirm=True)
    assert registry.get("invalid_confirm") is None


@pytest.mark.parametrize("risk_level", ["read", "self_write", "notify_self"])
def test_non_external_gate_uses_risk_level(risk_level, uid, monkeypatch):
    fn = Mock(return_value={"ok": True})
    confirm = Mock(side_effect=AssertionError("non-external tool must not require confirmation"))
    monkeypatch.setattr(registry, "_confirm", confirm)
    registry.register("local_tool", fn, risk_level=risk_level)
    assert registry.get("local_tool").requires_confirm is False
    registry.get("local_tool").requires_confirm = True
    assert registry.invoke_tool("local_tool", {}, {"user_id": uid}) == {"ok": True}
    confirm.assert_not_called()
    fn.assert_called_once()


def test_confirm_bound_to_owner_tool_args_and_single_use(uid):
    from app.confirm_store import issue_token
    fn = Mock(return_value={"ok": True})
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    registry.register("send", fn, risk_level="external", params_schema=schema)
    registry.register("other_send", fn, risk_level="external", params_schema=schema)
    args = {"text": "approved"}
    token = issue_token(registry.confirmation_scope("send", args), uid)
    ctx = {"user_id": uid, "confirm_token": token}
    from app.data_access import user_scope
    with user_scope(uid + "-other"):
        assert error(registry.invoke_tool("send", args, {**ctx, "user_id": uid + "-other"})) == "confirm_token_invalid"
    assert error(registry.invoke_tool("other_send", args, ctx)) == "confirm_token_invalid"
    assert error(registry.invoke_tool("send", {"text": "changed"}, ctx)) == "confirm_token_invalid"
    assert registry.invoke_tool("send", args, ctx) == {"ok": True}
    assert error(registry.invoke_tool("send", args, ctx)) == "confirm_token_used"
    assert fn.call_count == 1


def test_notify_self_allowed_and_logged(uid, monkeypatch):
    from app.channels import binding, outbound
    from app.agent.harness import traces
    binding.bind_channel("feishu", "self-recipient", uid)
    send = Mock(return_value={"ok": True})
    audit = Mock()
    monkeypatch.setattr(outbound, "deliver_channel_outbound", send)
    monkeypatch.setattr(traces, "record_step", audit)
    args = {"user_id": uid, "channel": "feishu", "external_user_id": "self-recipient", "text": "private reminder"}
    assert registry.invoke_tool("proactive.deliver", args, {"user_id": uid}) == {"ok": True}
    send.assert_called_once()
    row = audit.call_args.kwargs
    assert row["tool"] == "proactive.deliver" and row["ok"] is True
    assert row["latency_ms"] >= 0 and row["detail"]["confirmer"] == ""
    assert "private reminder" not in json.dumps(row)


@pytest.mark.parametrize("target", ["other", "unbound", "wrong_channel"])
def test_notify_self_targeting_other_or_unbound_refused(uid, monkeypatch, target):
    from app.channels import binding, outbound
    binding.bind_channel("feishu", "self-recipient", uid)
    binding.bind_channel("feishu", "other-recipient", uid + "-other")
    send = Mock()
    monkeypatch.setattr(outbound, "deliver_channel_outbound", send)
    args = {"user_id": uid, "channel": "feishu", "external_user_id": {
        "other": "other-recipient", "unbound": "missing", "wrong_channel": "self-recipient"}[target], "text": "reminder"}
    if target == "wrong_channel":
        args["channel"] = "wechat"
    assert error(registry.invoke_tool("proactive.deliver", args, {"user_id": uid})) == "notify_self_forbidden"
    send.assert_not_called()


def test_notify_self_cannot_choose_owner_from_args():
    uid = "untrusted-tool-caller"
    args = {"user_id": uid, "channel": "feishu", "external_user_id": "self-recipient", "text": "reminder"}
    for ctx in ({}, {"user_id": uid + "-other"}):
        assert error(registry.invoke_tool("proactive.deliver", args, ctx)) == "identity_required"


def test_proactive_job_cannot_declare_other_risk():
    with pytest.raises(ValueError, match="notify_self"):
        registry.register("proactive.bad", lambda: None, risk_level="external")


def test_decorated_direct_call_is_enforced(monkeypatch):
    from app.tools.writing import draft_text
    original = registry.invoke_tool
    invoke = Mock(wraps=original)
    monkeypatch.setattr(registry, "invoke_tool", invoke)
    out = draft_text(DRAFT_REQUEST, {"name": "Li Lei"})
    assert invoke.call_args.args[0] == "draft_text"
    assert out["kind"] == "email" and out["body"]
    with pytest.raises(registry.ToolError) as exc:
        draft_text(123, {})
    assert exc.value.code == "schema_validation"
    with pytest.raises(registry.ToolError) as exc:
        draft_text(DRAFT_REQUEST, {}, feedback="not a list")
    assert exc.value.code == "schema_validation"


def test_legacy_proactive_delivery_rechecks_binding(uid, monkeypatch):
    from app.agent.proactive import jobs
    from app.channels import binding, outbound
    # Simulate a stale or corrupt list result; resolve the actual owner at send time.
    monkeypatch.setattr(binding, "list_bindings", lambda **kw: [
        {"channel": "feishu", "external_user_id": "stranger"}])
    monkeypatch.setattr(binding, "resolve_user_id", lambda *a: uid + "-other")
    send = Mock()
    monkeypatch.setattr(outbound, "deliver_channel_outbound", send)
    with pytest.raises(registry.ToolError, match="本人"):
        jobs._try_deliver(uid, "reminder", kind="reminder")
    send.assert_not_called()


def test_bounded_input_repair_and_output_retries():
    fn = Mock(side_effect=["invalid", 4])
    registry.register("retry", fn, params_schema={"type": "object", "properties": {
        "value": {"type": "integer"}}, "required": ["value"]}, return_schema={"type": "integer"}, max_retries=1)
    repair = Mock(return_value={"value": 3})
    assert registry.invoke_tool("retry", {"value": "bad"}, {"repair_args": repair}) == 4
    assert repair.call_count == 1 and fn.call_count == 2
    assert repair.call_args.args[1]["code"] == "schema_validation"


def test_invalid_output_does_not_repeat_writes():
    fn = Mock(return_value="bad")
    registry.register("write", fn, risk_level="self_write", return_schema={"type": "object"}, max_retries=3)
    assert registry.invoke_tool("write", {})["status"] == "unknown"
    assert registry.get("write").max_retries == 0
    fn.assert_called_once()


def test_expired_deadline_never_executes():
    fn = Mock()
    registry.register("deadline", fn)
    assert error(registry.invoke_tool("deadline", {}, {"deadline": 0})) == "timeout"
    fn.assert_not_called()


def test_external_audit_redacts_token_and_records_confirmer(uid, monkeypatch):
    from app.agent.harness import traces
    from app.agent.harness.identity import user_pseudonym
    from app.confirm_store import issue_token
    registry.register("send", lambda: {"text": "private result"}, risk_level="external")
    token = issue_token(registry.confirmation_scope("send", {}), uid)
    audit = Mock()
    monkeypatch.setattr(traces, "record_step", audit)
    registry.invoke_tool("send", {}, {"user_id": uid, "confirm_token": token})
    row = audit.call_args.kwargs
    assert row["detail"]["confirmer"] == user_pseudonym(uid)
    assert token not in json.dumps(row) and "private result" not in json.dumps(row)


def test_api_tools_exposes_contracts(client):
    response = client.get("/api/tools")
    assert response.status_code == 200
    tools = {s["name"]: s for s in response.json()["data"]["tools"]}
    assert tools["proactive.deliver"]["risk_level"] == "notify_self"
    assert tools["schedule.run_now"]["risk_level"] == "notify_self"
    assert tools["draft_text"]["params_schema"]["properties"]["request"] == {"type": "string"}
    assert tools["draft_text"]["risk_level"] == "read"
    # Identity is never a model-visible parameter.
    assert all("user_id" not in item["params_schema"]["properties"] for item in tools.values())
    assert all(item["requires_confirm"] is (item["risk_level"] == "external") for item in tools.values())


def test_memory_revision_preserved_through_explicit_invocation(uid):
    from app import db
    before = (db.get_profile(uid) or {}).get("memory_rev", 0)
    out = registry.invoke_tool("memory_fact_update", {"user_id": uid, "fact_id": "city", "value": "上海"}, {"user_id": uid})
    assert "error" not in out
    assert db.get_profile(uid)["memory_rev"] == before + 1


def test_trace_redaction_lookup_does_not_recursively_enqueue(uid):
    from app.agent.harness import traces
    registry.invoke_tool("profile_read", {"user_id": uid}, {"user_id": uid})
    assert traces.flush(5.0)
    count = traces.writer_stats()["enqueued"]
    assert traces.flush(1.0)
    assert traces.writer_stats()["enqueued"] == count


def test_all_tools_have_reviewed_bilingual_contracts():
    import re
    tools = registry.list_tools()
    assert len(tools) == TOOL_COUNT
    assert {item['name'] for item in tools if item['name'].startswith('schedule.')} == SCHEDULE_TOOLS
    for item in tools:
        for field in ('description', 'when_to_use', 'when_not_to_use'):
            value = item[field]
            assert len(value) >= 35, (item['name'], field)
            assert re.search('[\u4e00-\u9fff]', value)
            assert re.search('[A-Za-z]{3,}', value)
            assert not re.search(r'placeholder|todo|tbd', value, re.I)
        assert item['description'] != item['when_to_use']
        def check(schema):
            assert schema, item['name']
            for child in schema.get('properties', {}).values():
                check(child)
            for child in schema.get('anyOf', []):
                check(child)
            if 'items' in schema:
                check(schema['items'])
        check(item['params_schema'])
        if item['risk_level'] != 'read':
            assert item['max_retries'] == 0
    assert len({s['timeout'] for s in tools}) >= 6
    assert any(s['max_retries'] > 0 for s in tools if s['risk_level'] == 'read')


@pytest.mark.parametrize('name,args', [
    ('schedule.update', {'schedule_id': 1, 'patch': 'not an object'}),
    ('schedule.create', {'schedule': ['not', 'an', 'object']}),
    ('profile_update', {'patch': 'name=Li Lei'}),
    ('memory_write', {'kind': 'note', 'summary': 'x', 'payload': 'arbitrary code'}),
    ('draft_text', {'request': DRAFT_REQUEST, 'profile': 'Li Lei'}),
    ('proactive.deliver', {'user_id': 'u', 'channel': 'web', 'external_user_id': 'u', 'text': 'hi', 'card': 42}),
])
def test_structured_parameters_reject_invalid_values(name, args):
    assert error(registry.invoke_tool(name, args)) == 'schema_validation'


def test_read_timeout_is_enforced_while_running():
    import threading
    import time
    release, finished = threading.Event(), threading.Event()
    def slow():
        try:
            release.wait(2)
            return 1
        finally:
            finished.set()
    registry.register('slow', slow, timeout=0.03)
    start = time.monotonic()
    try:
        assert error(registry.invoke_tool('slow', {})) == 'timeout'
        assert time.monotonic() - start < 0.5
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize('risk', ['self_write', 'notify_self', 'external'])
def test_write_timeout_records_unknown_then_completion(risk, uid, monkeypatch):
    import threading
    from app.agent.harness import traces
    from app.confirm_store import issue_token
    release, committed = threading.Event(), threading.Event()
    recorded = threading.Event()
    rows = []
    def audit(**kwargs):
        rows.append(kwargs)
        if kwargs.get('detail', {}).get('status') == 'completed':
            recorded.set()
    monkeypatch.setattr(traces, 'record_step', audit)
    def slow():
        committed.set()  # effect happens before response
        release.wait(2)
        return {'saved': True}
    registry.register('slow_write', slow, risk_level=risk, timeout=0.04, max_retries=5)
    ctx = {'user_id': uid}
    if risk == 'external':
        ctx['confirm_token'] = issue_token(registry.confirmation_scope('slow_write', {}), uid)
    try:
        out = registry.invoke_tool('slow_write', {}, ctx)
        assert committed.is_set()
        assert out['ok'] is None and out['status'] == 'unknown'
        assert out['message'] == '结果未确认，请到平台上查看，不要重复操作'
        assert 'verification' not in out
        assert rows[-1]['detail']['status'] == 'unknown'
        assert rows[-1]['error'] == 'status_unknown'
        assert rows[-1]['ok'] is None
        assert registry.get('slow_write').max_retries == 0
    finally:
        release.set()
        assert recorded.wait(1)
    assert rows[-1]['detail']['operation_id'] == out['operation_id']
    assert rows[-1]['detail']['status'] == 'completed'


def test_effect_followed_by_exception_is_unknown():
    effects = []
    def write():
        effects.append('committed')
        raise ConnectionError('response lost')
    registry.register('uncertain', write, risk_level='self_write', max_retries=3)
    out = registry.invoke_tool('uncertain', {})
    assert out['status'] == 'unknown' and out['ok'] is None
    assert effects == ['committed']


def test_nested_deadline_bounds_outer_wait_without_splitting_transaction():
    import threading
    release, finished = threading.Event(), threading.Event()
    threads = []
    def nested():
        threads.append(threading.get_ident())
        release.wait(2)
        return {'saved': True}
    def outer():
        threads.append(threading.get_ident())
        try:
            return registry.call('nested')
        finally:
            finished.set()
    registry.register('nested', nested, risk_level='self_write', timeout=0.03)
    registry.register('outer', outer, risk_level='self_write', timeout=2)
    try:
        assert registry.invoke_tool('outer', {})['status'] == 'unknown'
        assert len(set(threads)) == 1
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize('rate,random_value,logged', [('0', 0.0, False), ('1', .99, True),
    ('0.1', .09, True), ('0.1', .1, False), ('invalid', .2, False), ('2', .2, False)])
def test_trace_read_sampling(rate, random_value, logged, monkeypatch):
    from app.agent.harness import traces
    monkeypatch.setenv('TOOL_TRACE_READ_SAMPLE', rate)
    monkeypatch.setattr(registry.random, 'random', lambda: random_value)
    audit = Mock()
    monkeypatch.setattr(traces, 'record_step', audit)
    registry.register('sample', lambda: 1)
    assert registry.invoke_tool('sample', {}) == 1
    assert bool(audit.call_count) == logged
    registry.register('sample_write', lambda: 1, risk_level='self_write')
    assert registry.invoke_tool('sample_write', {}) == 1
    assert audit.call_args.kwargs['tool'] == 'sample_write'


def test_trace_read_default_is_ten_percent(monkeypatch):
    monkeypatch.delenv('TOOL_TRACE_READ_SAMPLE', raising=False)
    monkeypatch.setattr(registry.random, 'random', lambda: .099)
    assert registry._sample_read()
    monkeypatch.setattr(registry.random, 'random', lambda: .1)
    assert not registry._sample_read()


def test_transient_read_retries_but_not_effects():
    import httpx
    read = Mock(side_effect=[httpx.ReadTimeout('response lost'), {'value': 1}])
    registry.register('transport_read', read, max_retries=1)
    assert registry.invoke_tool('transport_read', {}) == {'value': 1}
    assert read.call_count == 2
    write = Mock(side_effect=httpx.ReadTimeout('response lost'))
    registry.register('transport_write', write, risk_level='self_write', max_retries=2)
    assert registry.invoke_tool('transport_write', {})['status'] == 'unknown'
    write.assert_called_once()


def test_unknown_audit_is_persisted_as_unknown(uid):
    from app.agent.harness import traces
    row = traces.record_step(thread_id='unknown-outcome', user_id=uid, tool='write', ok=None,
                             error='status_unknown', detail={'status': 'unknown'}, sync=True)
    assert row['ok'] is None
    stored = traces.list_traces('unknown-outcome')
    assert stored[-1]['ok'] is None
    assert stored[-1]['detail']['status'] == 'unknown'


@pytest.mark.parametrize("args", [{}, {"count": "bad"}, []])
def test_unavailable_precedes_validation_and_repair(args):
    fn, repair = Mock(), Mock()
    registry.register("offline_strict", fn, status="not_connected", max_retries=1,
                      params_schema={"type": "object", "properties": {
                          "count": {"type": "integer"}}, "required": ["count"]})
    out = registry.invoke_tool("offline_strict", args, {"repair_args": repair})
    assert error(out) == "not_connected"
    assert out["error"]["message"] == "暂不支持这项操作，相关服务尚未连接，未执行任何操作。"
    repair.assert_not_called()
    fn.assert_not_called()


@pytest.mark.parametrize("kind,message,diagnostic", [
    ("input", "提供的信息不符合要求，请检查后重试。", "Tool arguments do not match JSON Schema"),
    ("output", "返回的结果不符合要求，请稍后重试。", "Tool result does not match JSON Schema"),
    ("deadline", "操作等待超时，尚未开始执行。", "Tool deadline exceeded before execution"),
])
@pytest.mark.parametrize("raise_errors", [False, True])
def test_localized_refusals_keep_diagnostics_in_logs(kind, message, diagnostic, raise_errors, caplog):
    fn = Mock(return_value="private-result")
    registry.register("localized", fn, params_schema={"type": "object"},
                      return_schema={"type": "integer"})
    ctx = {"raise_errors": raise_errors}
    if kind == "deadline":
        ctx["deadline"] = 0
    args = "private-input" if kind == "input" else {}
    if raise_errors:
        with pytest.raises(registry.ToolError) as caught:
            registry.invoke_tool("localized", args, ctx)
        out = caught.value.as_dict()
    else:
        out = registry.invoke_tool("localized", args, ctx)
    assert out["error"]["message"] == message
    assert set(out["error"]) == {"code", "message"}
    assert diagnostic in caplog.text
    assert "private-input" not in caplog.text and "private-result" not in caplog.text
    if kind != "output":
        fn.assert_not_called()


def test_unknown_result_does_not_propagate_verification_claim():
    registry.register("uncertain_result", lambda: {"status": "unknown", "verification": "pending"},
                      risk_level="self_write")
    out = registry.invoke_tool("uncertain_result", {})
    assert out["status"] == "unknown"
    assert out["message"] == "结果未确认，请到平台上查看，不要重复操作"
    assert "verification" not in out


@pytest.fixture(autouse=True)
def _trusted_test_setup(request):
    if request.node.name == "test_notify_self_cannot_choose_owner_from_args":
        yield
        return
    from app.data_access import user_scope
    uid = request.getfixturevalue("uid")
    with user_scope(uid):
        yield
