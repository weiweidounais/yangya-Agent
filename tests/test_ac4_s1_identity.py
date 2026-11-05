"""AC4 stage 1: trusted identity and private persistence isolation."""
import sqlite3
import uuid

import pytest
from sqlalchemy import select
from langgraph.checkpoint.base import empty_checkpoint

from app import db, confirm_store
from app.agent import tool_registry
from app.agent.memory import episodic
from app.channels import conversation
from app.channels.binding import bind_channel
from app.channels.base import ChannelOption
from app.data_access import IdentityRequired, NotFound, current_user, system_scope, user_scope
from app.data_access.checkpoints import UserSqliteSaver
from app.data_access.session import scope_flush


@pytest.fixture
def owners():
    return tuple(uuid.uuid4().hex for _ in range(2))


def test_trace_writer_redacts_only_record_owner_sandbox(owners, monkeypatch):
    import threading

    from app.agent.harness import traces

    a, b = owners
    values = {a: "sandbox-albatross", b: "sandbox-butterfly"}
    for uid, value in values.items():
        with user_scope(uid):
            db.save_profile(uid, {"privacy_sandbox": [value]})
    assert traces.flush()
    reads = []
    get_profile = db.read_profile_data

    def checked_get_profile(user_id):
        reads.append((user_id, current_user(), threading.current_thread().name))
        return get_profile(user_id)

    monkeypatch.setattr(db, "read_profile_data", checked_get_profile)
    tool_calls = []
    monkeypatch.setattr(tool_registry, "invoke_tool", lambda *a, **kw: tool_calls.append((a, kw)))
    tids = []
    for uid in (a, b, a, ""):
        tid = uuid.uuid4().hex
        tids.append((tid, uid))
        assert traces.record_step(
            thread_id=tid, user_id=uid, sync=False,
            input_summary=" ".join(values.values()),
        ) == {"queued": True}
    assert traces.flush()
    assert reads == [(uid, uid, "trace-writer") for uid in (a, b, a)]
    assert tool_calls == []
    for tid, uid in tids:
        rows = traces.query_traces(thread_id=tid)["traces"]
        assert len(rows) == 1
        for owner, value in values.items():
            assert (value in rows[0]["input_summary"]) == (owner != uid)
    with pytest.raises(IdentityRequired):
        current_user()


def test_profile_data_read_requires_identity_and_hides_foreign_owner(owners):
    a, b = owners
    with user_scope(b):
        db.save_profile(b, {"name": "other-owner"})
        assert db.read_profile_data(b) == {"name": "other-owner"}
    with pytest.raises(IdentityRequired):
        db.read_profile_data(b)
    with user_scope(a):
        assert db.read_profile_data(b) == db.read_profile_data(uuid.uuid4().hex) == {}


def test_identity_restores_and_trace_context_is_not_authority():
    from app.agent.harness.context import step_context
    with pytest.raises(IdentityRequired):
        current_user()
    with user_scope("A"):
        with user_scope("B"):
            assert current_user() == "B"
        assert current_user() == "A"
    with step_context(user_id="forged"):
        result = tool_registry.invoke_tool("profile_read", {"user_id": "forged"}, {"user_id": "forged"})
        assert result["error"]["code"] == "identity_required"


def test_private_entrypoints_require_identity():
    for call in (
        lambda: db.get_profile("A"),
        lambda: episodic.get_event(123),
        lambda: confirm_store.get_decision("missing"),
        lambda: conversation.get_pending("test", "missing"),
    ):
        with pytest.raises((IdentityRequired, tool_registry.ToolError)):
            call()
    with db.SessionLocal() as session:
        session.add(db.UserProfile(user_id="A", profile_json="{}"))
        with pytest.raises(IdentityRequired):
            scope_flush(session, None, None)


def test_profile_and_tool_arguments_use_only_context(owners):
    a, b = owners
    with user_scope(a):
        db.save_profile(a, {"city": "A"})
    with user_scope(b):
        db.save_profile(b, {"city": "B"})
        assert tool_registry.invoke_tool("profile_read", {"user_id": a, "owner_id": a}, {"user_id": a})["city"] == "B"
        tool_registry.invoke_tool("profile_update", {"user_id": a, "patch": {"city": "B2"}})
    with user_scope(a):
        assert db.get_profile(a)["city"] == "A"
        with db.SessionLocal() as session:
            assert session.get(db.UserProfile, b) is None
            assert session.execute(select(db.UserProfile).where(db.UserProfile.user_id == b)).scalar_one_or_none() is None
    for spec in tool_registry.list_tools():
        assert not {"user_id", "owner_id"} & spec["params_schema"].get("properties", {}).keys()


def test_memory_confirm_isolation(owners):
    a, b = owners
    with user_scope(a):
        event = episodic.write_event(a, "note", "private-A")
        confirm_store.record_decision(a, a, True)
    with user_scope(b):
        assert episodic.get_event(event["id"]) == episodic.get_event(-9999)
        assert confirm_store.get_decision(a) == confirm_store.get_decision("missing")
        assert "private-A" not in str(episodic.search_events(b))


def test_pending_uses_binding_not_supplied_owner(owners):
    a, b = owners
    with system_scope():
        bind_channel("test", a, a)
    with system_scope():
        bind_channel("test", b, b)
    with user_scope(a):
        conversation.set_pending("test", a, kind="confirm", options=[ChannelOption(id="yes", label="Yes", value={"confirmed": True})])
    with user_scope(b):
        assert conversation.get_pending("test", b) == {}
        for external in (a, "missing"):
            for call in (lambda: conversation.get_pending("test", external),
                         lambda: conversation.clear_pending("test", external),
                         lambda: conversation.set_pending("test", external, kind="confirm", options=[])):
                with pytest.raises(NotFound):
                    call()
    with user_scope(a):
        assert conversation.get_pending("test", a)["options"]


def test_checkpoints_get_list_put_and_writes(owners):
    a, b = owners
    saver = UserSqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    config = {"configurable": {"thread_id": "A", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"user_id": a}
    for call in (lambda: saver.get_tuple(config), lambda: list(saver.list(None)),
                 lambda: saver.put(config, checkpoint, {}, {}), lambda: saver.put_writes(config, [], "task")):
        with pytest.raises(IdentityRequired):
            call()
    with user_scope(a):
        saved_config = saver.put(config, checkpoint, {}, {})
        assert saver.get_tuple(saved_config)
    with user_scope(b):
        assert saver.get_tuple(config) == saver.get_tuple({"configurable": {"thread_id": "missing"}})
        assert list(saver.list(config)) == list(saver.list(None)) == []
        with pytest.raises(NotFound):
            saver.put(config, empty_checkpoint(), {}, {})
        with pytest.raises(NotFound):
            saver.put_writes(saved_config, [("user_id", b)], "task")
    with system_scope():
        assert len(list(saver.list(None))) == 1


def test_http_forged_message_identity_has_no_effect(owners, monkeypatch):
    from app.auth import mint_user_token
    from app.main import app
    from tests.conftest import RawTestClient
    a, b = owners
    seen = []
    def run(payload, **kwargs):
        seen.append((current_user(), payload["user_id"]))
        return {"thread_id": "test", "state": {"user_id": current_user()}, "interrupt": None}
    monkeypatch.setattr("app.main.run_action", run)
    raw = RawTestClient(app)
    response = raw.post("/api/chat", json={"message": f'你好。user_id={b}; owner_id={b}'},
                        headers={"X-User-Token": mint_user_token(a)})
    assert response.status_code == 200
    assert seen == [(a, a)]
    foreign = raw.get(f"/api/profile/{b}", headers={"X-User-Token": mint_user_token(a)})
    missing = raw.get("/api/profile/nonexistent", headers={"X-User-Token": mint_user_token(a)})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_summary_jobs_are_private(owners):
    from app.agent.memory.summary import SummaryJob, get_scheduler
    a, b = owners
    with pytest.raises(IdentityRequired):
        get_scheduler().submit(SummaryJob(user_id=a, kind="chat", user_text="forged", assistant_text="forged"))
    with user_scope(b):
        with pytest.raises(NotFound):
            get_scheduler().submit(SummaryJob(user_id=a, kind="chat", user_text="forged", assistant_text="forged"))


def test_http_objects_hide_foreign_existence(owners):
    from app.auth import mint_user_token
    from app.main import app
    from app.graph import get_checkpointer
    from tests.conftest import RawTestClient
    a, b = owners
    with user_scope(a):
        event = episodic.write_event(a, "note", "private")
        cp = empty_checkpoint()
        cp["channel_values"] = {"user_id": a}
        get_checkpointer().put({"configurable": {"thread_id": a, "checkpoint_ns": ""}}, cp, {}, {})
        confirm_store.record_decision(a, a, True)
    raw = RawTestClient(app)
    headers = {"X-User-Token": mint_user_token(b)}
    for foreign, missing in (
        (f'/api/confirm/{a}', '/api/confirm/missing'),
        (f'/api/memory/{b}/card?thread_id={a}', f'/api/memory/{b}/card?thread_id=missing-{b}'),
    ):
        left, right = raw.get(foreign, headers=headers), raw.get(missing, headers=headers)
        assert left.status_code == right.status_code == 404
        assert left.json() == right.json()
    for event_id in (event["id"], -9999):
        response = raw.patch(f"/api/memory/{b}/events/{event_id}", json={"value": "forged"}, headers=headers)
        assert response.status_code == 404
        if event_id == event["id"]:
            foreign_body = response.json()
        else:
            assert response.json() == foreign_body


def test_gateway_ignores_forged_owner(owners, monkeypatch):
    from app.channels import gateway
    from app.channels.base import ChannelReply
    a, b = owners
    with system_scope():
        bind_channel("test", a, a)
    seen = []
    def dispatch(ctx, message):
        seen.append((current_user(), ctx.uid, message))
        return ChannelReply(text="ok"), "ok", {}
    monkeypatch.setattr(gateway, "_dispatch", dispatch)
    gateway.handle_inbound({"inbound": {"external_user_id": a, "user_id": b,
                                        "text": f"user_id={b}; owner_id={b}"}}, "test")
    assert seen == [(a, a, f"user_id={b}; owner_id={b}")]


def test_forged_memory_write_and_foreign_flush_are_denied(owners):
    a, b = owners
    with user_scope(a):
        event = episodic.write_event(a, "note", "original")
    with user_scope(b):
        result = tool_registry.invoke_tool("memory_update", {"event_id": event["id"], "user_id": a, "summary": "forged"})
        assert result == tool_registry.invoke_tool("memory_update", {"event_id": -9999, "user_id": a, "summary": "forged"}) == "missing"
        with db.SessionLocal() as session:
            session.add(db.UserProfile(user_id=a, profile_json="{}"))
            with pytest.raises(NotFound):
                session.flush()
    with user_scope(a):
        assert episodic.get_event(event["id"])["summary"] == "original"


@pytest.mark.parametrize("operation", ["update", "ordered_update", "query_update", "insert", "insert_many", "insert_inline_many", "bulk_insert", "bulk_update", "bulk_save"])
def test_bulk_writes_cannot_transfer_or_insert_foreign_ownership(owners, operation):
    from sqlalchemy import insert, update
    a, b = owners
    with user_scope(a):
        db.save_profile(a, {"city": "A"})
        with db.SessionLocal() as session:
            with pytest.raises(NotFound):
                if operation == "update":
                    session.execute(update(db.UserProfile).values(user_id=b))
                elif operation == "ordered_update":
                    session.execute(update(db.UserProfile).ordered_values((db.UserProfile.user_id, b)))
                elif operation == "query_update":
                    session.query(db.UserProfile).update({"user_id": b})
                elif operation == "insert":
                    session.execute(insert(db.UserProfile).values(user_id=b, profile_json="{}"))
                elif operation == "insert_many":
                    session.execute(insert(db.UserProfile), [{"user_id": a}, {"user_id": b}])
                elif operation == "insert_inline_many":
                    session.execute(insert(db.UserProfile).values([{"user_id": a}, {"user_id": b}]))
                elif operation == "bulk_insert":
                    session.bulk_insert_mappings(db.UserProfile, [{"user_id": b}])
                elif operation == "bulk_update":
                    session.bulk_update_mappings(db.UserProfile, [{"user_id": b, "profile_json": "{}"}])
                else:
                    session.bulk_save_objects([db.UserProfile(user_id=b, profile_json="{}")])
            session.rollback()
        assert db.get_profile(a)["city"] == "A"
    with user_scope(b):
        assert db.get_profile(b) == {}


def test_owned_bulk_insert_and_update(owners):
    from sqlalchemy import insert, update
    a, b = owners
    with user_scope(a), db.SessionLocal() as session:
        session.execute(insert(db.UserProfile), [{"user_id": a, "profile_json": '{}'}])
        session.commit()
    with user_scope(b), db.SessionLocal() as session:
        session.execute(update(db.UserProfile).values(profile_json='{"city":"B"}'))
        session.commit()
    with user_scope(a):
        assert db.get_profile(a) == {}


def test_system_scope_does_not_authorize_private_tools_from_arguments(owners):
    a, b = owners
    with system_scope():
        for name, args in (("profile_read", {"user_id": a}),
                           ("profile_update", {"owner_id": a, "patch": {"city": "forged"}})):
            out = tool_registry.invoke_tool(name, args, {"user_id": b})
            assert out["error"]["code"] == "identity_required"
        with user_scope(a):
            tool_registry.invoke_tool("profile_update", {"user_id": b, "patch": {"city": "A"}})
            assert db.get_profile(a)["city"] == "A"
    with user_scope(b):
        assert db.get_profile(b) == {}


def test_chat_and_confirm_foreign_missing_status_and_body_match(owners):
    from app.auth import mint_user_token
    from app.main import app
    from app.graph import get_checkpointer
    from tests.conftest import RawTestClient
    a, b = owners
    with user_scope(a):
        cp = empty_checkpoint()
        cp["channel_values"] = {"user_id": a}
        get_checkpointer().put({"configurable": {"thread_id": a, "checkpoint_ns": ""}}, cp, {}, {})
    raw = RawTestClient(app)
    headers = {"X-User-Token": mint_user_token(b)}
    for endpoint in ("/api/chat", "/api/confirm"):
        responses = [raw.post(endpoint, headers=headers, json={"thread_id": tid, "message": "hello", "confirmed": True})
                     for tid in (a, "missing-" + b)]
        assert responses[0].status_code == responses[1].status_code == 404
        assert responses[0].json() == responses[1].json()


def test_memory_card_and_schedule_tool_isolation(owners):
    from app.agent.memory import card
    from app.agent.schedules import service
    a, b = owners
    with user_scope(a):
        db.save_profile(a, {"city": "private-A"})
        event = episodic.write_event(a, "note", "private-A")
        schedule = service.mutate(a, "create", {
            "name": "private-A", "job_kind": "custom_nl",
            "trigger": {"type": "cron", "expr": "0 8 * * *"}, "prompt_text": "私有提醒",
        })
    try:
        with user_scope(b):
            view = tool_registry.invoke_tool("memory_card", {"user_id": a})
            assert "private-A" not in str(view)
            for event_id in (event["id"], -9999):
                with pytest.raises(card.MemoryAccessError) as exc:
                    card.update_episode(a, event_id, "forged")
                assert (exc.value.http_status, exc.value.code) == (404, "not_found")
            for sid in (schedule["id"], -9999):
                with pytest.raises(NotFound):
                    service.get_schedule(b, sid)
            assert "private-A" not in str(tool_registry.invoke_tool("schedule.list", {"user_id": a}))
            # A foreign schedule is indistinguishable from a missing one.
            outcomes = [tool_registry.invoke_tool("schedule.pause", {"user_id": a, "schedule_id": sid})
                        for sid in (schedule["id"], -9999)]
            for outcome in outcomes:
                outcome.pop("operation_id", None)
                assert outcome["ok"] is None and outcome["status"] == "unknown"
            assert outcomes[0] == outcomes[1]
        for name, args in (("memory_card", {"user_id": a}),
                           ("memory_episode_update", {"user_id": a, "event_id": event["id"], "value": "forged"}),
                           ("schedule.list", {"user_id": a}),
                           ("schedule.pause", {"user_id": a, "schedule_id": schedule["id"]})):
            assert tool_registry.invoke_tool(name, args)["error"]["code"] == "identity_required"
        with user_scope(a):
            assert episodic.get_event(event["id"])["summary"] == "private-A"
            assert service.get_schedule(a, schedule["id"])["status"] == "active"
    finally:
        with user_scope(a):
            service.mutate(a, "delete", {"id": schedule["id"]})


def test_background_summary_runs_tools_and_writes_as_owner(owners):
    from app.agent.memory.summary import SummaryJob, SummaryScheduler
    a, b = owners
    scheduler = SummaryScheduler(retry_delay=0, max_attempts=1)
    seen = []
    def generate(job):
        seen.append((current_user(), tool_registry.invoke_tool("profile_read", {"user_id": b})))
        return "用户希望远程工作"
    scheduler.set_generator(generate)
    try:
        with pytest.raises(IdentityRequired):
            scheduler.submit(SummaryJob(a, "chat", "hi", "hello"))
        with user_scope(a):
            db.save_profile(a, {"city": "A"})
            scheduler.submit(SummaryJob(a, "chat", "hi", "hello"))
        with user_scope(b):
            assert scheduler.flush(5)
            assert db.get_profile(b) == {}
            assert episodic.search_events(b) == []
        assert seen == [(a, {"city": "A"})]
        with user_scope(a):
            assert db.get_profile(a)["dialogue_summary"] == "用户希望远程工作"
            assert episodic.search_events(a)
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/draft"])
@pytest.mark.parametrize("mode", ["rules", "hybrid"])
def test_real_http_graph_propagates_identity_to_tool_worker(owners, monkeypatch, endpoint, mode):
    from dataclasses import replace
    from app.auth import mint_user_token
    from app.main import app
    from tests.conftest import RawTestClient
    a, b = owners
    from app.agent import intent_scoring
    monkeypatch.setenv("INTENT_SCORING_MODE", mode)
    monkeypatch.setattr(intent_scoring, "score_text", lambda _: (
        {"chat": .2}, {"confidence": .2, "margin": 0., "model_version": "mock", "exemplar_version": "test"}))
    spec = tool_registry.get("profile_read")
    seen = []
    def read(user_id):
        seen.append((current_user(), user_id))
        return spec.fn(user_id)
    monkeypatch.setitem(tool_registry._REGISTRY, "profile_read", replace(spec, fn=read))
    with pytest.raises(IdentityRequired):
        current_user()
    response = RawTestClient(app).post(endpoint, headers={"X-User-Token": mint_user_token(a)},
        json={"message": "你好", "request": "帮我写一封请假邮件，周五请一天假", "owner_id": b})
    assert response.status_code == 200, response.text
    assert seen and all(pair == (a, a) for pair in seen)
    with pytest.raises(IdentityRequired):
        current_user()


def test_intent_gateway_masks_only_trusted_owner_before_graph(owners, monkeypatch):
    import httpx
    from app.agent.harness.context import step_context
    from app.gateway import llm_gateway, core
    a, b = owners
    values = {a: "sandbox-albatross", b: "sandbox-butterfly"}
    for uid, value in values.items():
        with user_scope(uid):
            db.save_profile(uid, {"privacy_sandbox": [value]})
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-only")
    seen, reads = [], []
    read_profile = db.read_profile_data
    def read(uid):
        reads.append((current_user(), uid))
        return read_profile(uid)
    monkeypatch.setattr(db, "read_profile_data", read)
    def post(url, **kwargs):
        seen.append((current_user(), kwargs["json"]["input"][0]))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1., 0.]}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    def complete(system, user, **kwargs):
        seen.append((current_user(), user))
        return core.Result(content="A", logprobs={"content": [{"token": "A", "logprob": -.01}]})
    monkeypatch.setattr(core, "complete", complete)
    text = " ".join(values.values())
    for uid, forged in ((a, b), (b, a)):
        with user_scope(uid), step_context(user_id=forged):
            assert llm_gateway.embed_texts([text], model="mock", base_url="https://invalid.test") == [[1., 0.]]
            assert llm_gateway.classify_label(text, ("chat",))[0] == "chat"
            assert current_user() == uid
        expected = text.replace(values[uid], "[隐私已隐藏]")
        assert seen[-2:] == [(uid, expected), (uid, expected)]
    assert reads == [(a, a), (a, a), (b, b), (b, b)]
    with pytest.raises(IdentityRequired):
        current_user()


def test_graph_business_workspace_keeps_private_session_guards(owners):
    from app.business_stage import stage_business_writes
    a, b = owners
    with user_scope(a):
        db.save_profile(a, {"city": "A"})
    with user_scope(b), stage_business_writes():
        with db.memory_session() as session:
            assert session.get(db.UserProfile, a) is None
            session.add(db.UserProfile(user_id=a, profile_json="{}"))
            with pytest.raises(NotFound):
                session.flush()
            session.expunge_all()
    with stage_business_writes(), db.memory_session() as session:
        with pytest.raises(IdentityRequired):
            session.get(db.UserProfile, a)


def test_channel_delivery_reestablishes_binding_identity(owners, monkeypatch, mock_text_channel):
    from app.channels import gateway, outbound
    from app.channels.base import ChannelReply
    a, b = owners
    with system_scope():
        bind_channel("mock_text", "delivery-recipient", a)
    seen = []
    def guard(rendered, *, user_id, **kwargs):
        seen.append((current_user(), user_id))
        assert tool_registry.invoke_tool("profile_read", {"user_id": b}) == {"city": "A"}
        return rendered
    monkeypatch.setattr(gateway, "guard_rendered", guard)
    monkeypatch.setattr(outbound, "guard_rendered", guard)
    with user_scope(a):
        db.save_profile(a, {"city": "A"})
    # Ingress already returned; no caller context is present at delivery time.
    gateway.deliver_result("mock_text", {"user_id": b}, {
        "outbound": {"external_user_id": "delivery-recipient", "user_id": b, "text": "hello"}})
    gateway.notify("mock_text", "delivery-recipient", ChannelReply(text="bound"))
    assert seen == [(a, a), (a, a)]
    assert len(mock_text_channel.sent) == 2
    with pytest.raises(IdentityRequired):
        current_user()


def test_summary_tool_ignores_forged_owner_and_requires_context(owners, monkeypatch):
    from app.agent.memory import summary
    a, b = owners
    seen = []
    def submit(job):
        seen.append((current_user(), job.user_id))
        return {"ok": True, "scheduled": True}
    monkeypatch.setattr(summary.get_scheduler(), "submit", submit)
    args = {"user_id": b, "kind": "chat", "user_text": "hello", "assistant_text": "hi"}
    assert tool_registry.invoke_tool("summarize_turn", args)["error"]["code"] == "identity_required"
    with user_scope(a):
        assert tool_registry.invoke_tool("summarize_turn", args)["scheduled"] is True
    assert seen == [(a, a)]


def test_scheduler_opens_explicit_user_scopes_for_private_tools(owners, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete, update
    from app.agent.proactive import jobs
    from app.agent.proactive.config import ProactiveConfig
    from app.agent.schedules import runner, service
    from app.agent.schedules.models import Schedule, ScheduleRun
    seen = []
    def run(user_id, schedule_id, *, at=None, cfg=None):
        seen.append((current_user(), user_id))
        return {"ok": True, "user_id": user_id}
    monkeypatch.setattr(runner, "run_schedule", run)
    monkeypatch.setattr(jobs, "list_candidate_user_ids", lambda **kwargs: list(owners))
    overdue = datetime.now(timezone.utc) - timedelta(minutes=5)
    try:
        for uid in owners:
            with user_scope(uid):
                row = service.mutate(uid, "create", {
                    "name": "合成任务", "job_kind": "custom_nl",
                    "trigger": {"type": "cron", "expr": "0 8 * * *"}, "prompt_text": "合成指令",
                })
                with db.SessionLocal() as session:
                    session.execute(update(Schedule).where(Schedule.id == row["id"]).values(next_run_at=overdue))
                    session.commit()
        with system_scope():
            result = jobs.run_tick(ProactiveConfig(enabled=True))
            assert current_user() is None
        assert result["ok"]
        assert sorted(seen) == sorted((uid, uid) for uid in owners)
    finally:
        for uid in owners:
            with user_scope(uid), db.SessionLocal() as session, session.begin():
                for model in (ScheduleRun, Schedule):
                    session.execute(delete(model).where(model.user_id == uid))


@pytest.mark.parametrize("existing_where", [False, True])
def test_upsert_conflict_update_is_scoped_to_existing_owner(owners, existing_where):
    from sqlalchemy.dialects.sqlite import insert
    a, b = owners
    with user_scope(b), db.SessionLocal() as session:
        row = episodic.EpisodicEvent(user_id=b, kind="note", summary="original")
        session.add(row)
        session.commit()
        row_id = row.id
    statement = insert(episodic.EpisodicEvent).on_conflict_do_update(
        index_elements=[episodic.EpisodicEvent.id], set_={"summary": "updated"},
        where=(episodic.EpisodicEvent.summary == "original") if existing_where else None,
    )
    for uid, expected in ((a, "original"), (b, "updated")):
        with user_scope(uid), db.SessionLocal() as session:
            session.execute(statement.values(id=row_id, user_id=uid, kind="note", summary="inserted"))
            session.commit()
        with user_scope(b), db.SessionLocal() as session:
            row = session.get(episodic.EpisodicEvent, row_id)
            assert (row.user_id, row.summary) == (b, expected)
    if existing_where:
        with user_scope(b), db.SessionLocal() as session:
            # The caller's false predicate must still suppress an owned update.
            session.execute(insert(episodic.EpisodicEvent).values(id=row_id, user_id=b, kind="note").on_conflict_do_update(
                index_elements=[episodic.EpisodicEvent.id], set_={"summary": "unexpected"},
                where=episodic.EpisodicEvent.summary == "original"))
            session.commit()
            assert session.get(episodic.EpisodicEvent, row_id).summary == "updated"


@pytest.mark.parametrize("column_key", [False, True])
@pytest.mark.parametrize("owner_value", ["foreign", "excluded"])
def test_upsert_cannot_assign_owner(owners, column_key, owner_value):
    from sqlalchemy.dialects.sqlite import insert
    a, b = owners
    with user_scope(a), db.SessionLocal() as session:
        row = episodic.EpisodicEvent(user_id=a, kind="note", summary="original")
        session.add(row)
        session.commit()
        row_id = row.id
        statement = insert(episodic.EpisodicEvent).values(id=row_id, user_id=a, kind="note")
        key = episodic.EpisodicEvent.user_id if column_key else "user_id"
        value = b if owner_value == "foreign" else statement.excluded.user_id
        with pytest.raises(NotFound):
            session.execute(statement.on_conflict_do_update(
                index_elements=[episodic.EpisodicEvent.id], set_={key: value, "summary": "forged"}))
        session.rollback()
        row = session.get(episodic.EpisodicEvent, row_id)
        assert (row.user_id, row.summary) == (a, "original")
    with user_scope(b), db.SessionLocal() as session:
        assert session.get(episodic.EpisodicEvent, row_id) is None
