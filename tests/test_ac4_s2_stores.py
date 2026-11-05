"""Adversarial stage-2 coverage; HTTP cases are run by the lead outside the sandbox."""
import importlib
import struct
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, inspect, select, update

from app import db
from app.data_access import NotFound, user_scope, system_scope
from app.data_access.session import SessionLocal, _PRIVATE_TABLES, owner_column

MODULES = (
    "app.channels.binding", "app.channels.links", "app.channels.conversation",
    "app.agent.proactive.store", "app.agent.memory.episodic", "app.confirm_store",
    "app.agent.schedules.models", "app.data_access.memory_facts",
    "app.data_access.memory_migration", "app.turn_results", "app.secrets_vault",
)
for module in MODULES:
    importlib.import_module(module)
MODELS = sorted((m.class_ for m in db.Base.registry.mappers
                 if owner_column(m.class_) is not None), key=lambda m: m.__tablename__)


def make_row(model, owner):
    values = {owner_column(model).key: owner}
    if model.__tablename__ == "memory_facts":
        # A required vector BLOB needs valid data, like the scalar types below.
        values["embedding"] = struct.pack("<1024f", 1.0, *([0.0] * 1023))
    for column in model.__table__.columns:
        if column.key in values or column.default is not None or column.nullable:
            continue
        if column.primary_key and column.type.python_type is int:
            continue
        kind = column.type.python_type
        values[column.key] = (uuid.uuid4().hex if kind is str else
                              datetime.now(timezone.utc) if kind is datetime else
                              False if kind is bool else 0)
    return model(**values)


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__tablename__)
def test_each_store_read_update_delete_and_forged_insert(model):
    """The session actually used by stores filters IDs even without caller predicates."""
    db.init_db()
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    column = owner_column(model)
    with user_scope(a), SessionLocal.begin() as session:
        row = make_row(model, a)
        session.add(row)
        session.flush()
        identity = inspect(row).identity
        pk = list(model.__table__.primary_key.columns)
        predicate = [getattr(model, c.key) == v for c, v in zip(pk, identity)]
    with user_scope(b), SessionLocal.begin() as session:
        assert session.get(model, identity) is None
        assert session.scalars(select(model).where(*predicate)).all() == []
        field = next(c for c in model.__table__.columns
                     if not c.primary_key and c.key != column.key)
        assert session.execute(update(model).where(*predicate).values(
            {field.key: getattr(model, field.key)})).rowcount == 0
        assert session.execute(delete(model).where(*predicate)).rowcount == 0
    with user_scope(b), SessionLocal() as session:
        session.add(make_row(model, a))
        with pytest.raises(NotFound):
            session.flush()
    with user_scope(a), SessionLocal.begin() as session:
        assert session.get(model, identity) is not None
        assert session.execute(delete(model).where(*predicate)).rowcount == 1
        assert session.get(model, identity) is None


def test_private_table_inventory_is_complete():
    assert {m.__tablename__ for m in MODELS} | {"channel_conversations"} == _PRIVATE_TABLES


def test_conversation_binding_filter_is_inside_session():
    from app.channels import binding, conversation
    from app.channels.base import ChannelOption
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with user_scope(a):
        binding.bind_channel("s2", a, a)
        conversation.set_pending("s2", a, kind="confirm", options=[ChannelOption(id="yes", label="Yes", value={})])
        with SessionLocal() as session:
            row = session.scalar(select(conversation.ChannelConversation).where(
                conversation.ChannelConversation.external_user_id == a))
            key = row.id
    with user_scope(b):
        assert binding.get_binding("s2", a) == binding.get_binding("s2", "missing")
        assert not binding.unbind_channel("s2", a)
        with SessionLocal.begin() as session:
            model = conversation.ChannelConversation
            assert session.get(model, key) is None
            assert session.scalars(select(model).where(model.id == key)).all() == []
            assert session.execute(update(model).where(model.id == key).values(pending_json="{}")).rowcount == 0
            assert session.execute(delete(model).where(model.id == key)).rowcount == 0
        for external in (a, "missing"):
            for call in (conversation.get_pending, conversation.clear_pending):
                with pytest.raises(NotFound):
                    call("s2", external)
    with user_scope(a):
        assert conversation.get_pending("s2", a)["options"]
        conversation.clear_pending("s2", a)
        assert conversation.get_pending("s2", a) == {}


def test_forged_tool_ids_do_not_change_identity():
    from app.agent.tool_registry import invoke_tool
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    for uid in (a, b):
        with user_scope(uid):
            db.save_profile(uid, {"city": uid})
    with user_scope(b):
        assert invoke_tool("profile_read", {"user_id": a, "owner_id": a})["city"] == b
        invoke_tool("profile_update", {"user_id": a, "patch": {"city": "changed"}})
    with user_scope(a):
        assert db.get_profile(a)["city"] == a


def test_file_and_cache_namespaces(tmp_path):
    from app.data_access.namespaces import user_file, user_key
    cache = {}
    with user_scope("A/../owner"):
        a = user_file(tmp_path, "export.json")
        a.parent.mkdir()
        a.write_text("private A")
        cache[user_key("summary", "shared-thread")] = "private A"
    with user_scope("B"):
        b = user_file(tmp_path, "export.json")
        assert a != b and not b.exists()
        assert cache.get(user_key("summary", "shared-thread")) is None
        with pytest.raises(NotFound):
            user_file(tmp_path, "../" + a.parent.name + "/export.json")
        with pytest.raises(NotFound):
            user_file(tmp_path, str(a))


def test_summary_queue_uses_current_user_and_copies_job(monkeypatch):
    from app.agent.memory.summary import SummaryJob, SummaryScheduler
    from app.data_access.namespaces import user_key
    scheduler = SummaryScheduler()
    monkeypatch.setattr(scheduler, "_ensure_worker", lambda: None)
    with user_scope("A"):
        job = SummaryJob(user_id="A", kind="chat", user_text="A", assistant_text="", thread_id="shared")
        scheduler.submit(job)
        key_a = user_key("summary", "shared")
        job.user_id = "B"
        assert scheduler._pending[key_a].user_id == "A"
    with user_scope("B"):
        assert scheduler._pending.get(user_key("summary", "shared")) is None
        with pytest.raises(NotFound):
            scheduler.submit(SummaryJob(user_id="A", kind="chat", user_text="", assistant_text=""))


def test_store_entrypoints_hide_foreign_ids_and_reject_forged_owners():
    from app.channels import links
    from app.agent.proactive import store
    from app.agent.schedules import service
    from app import confirm_store, secrets_vault
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with user_scope(a):
        link = links.issue("sandbox", channel="s2", external_user_id=a,
                           user_id=a, ttl_seconds=600)
        item = store.create_item(a, "test", body="private A")
        store.record_run(a, "test", "today")
        token = confirm_store.issue_token(a, a)
        schedule = service.mutate(a, "create", {
            "name": "private A", "job_kind": "custom_nl",
            "trigger": {"type": "cron", "expr": "0 8 * * *"}, "prompt_text": "private A",
        })
        secrets_vault.save_secret(a, "phone", {"phone": "13900001111"})
    with user_scope(b):
        assert links.peek(link, "sandbox") == links.peek("missing", "sandbox") is None
        assert links.consume(link, "sandbox") is None
        assert links.revoke_for("s2", a, "sandbox") == 0
        assert store.item_owner(item["id"]) == store.item_owner(-1) is None
        assert store.mark_item(item["id"], status="dismissed") is None
        assert not store.has_run(a, "test", "today")
        assert secrets_vault.list_secrets(a) == []
        assert secrets_vault.load_secret_values(a, "phone") is None
        assert not secrets_vault.delete_secret(a, "phone")
        assert service.list_schedules(a) == []
        for ident in (schedule["id"], -1):
            for call in (lambda: service.get_schedule(a, ident),
                         lambda: service.mutate(a, "pause", {"id": ident}),
                         lambda: service.mutate(a, "delete", {"id": ident})):
                with pytest.raises(NotFound):
                    call()
        for value in (token, "missing"):
            with pytest.raises(confirm_store.ConfirmTokenError) as error:
                confirm_store.consume_token(value, a, a)
            assert (error.value.http_status, error.value.code) == (404, "confirm_token_invalid")
        with pytest.raises(NotFound):
            store.create_item(a, "test")
    with user_scope(a):
        assert links.peek(link, "sandbox") is not None
        assert store.item_owner(item["id"]) == a
        assert service.get_schedule(a, schedule["id"])["status"] == "active"
        assert "private A" in str(service.list_schedules(a))
        service.mutate(a, "delete", {"id": schedule["id"]})
        assert secrets_vault.delete_secret(a, "phone")


def test_core_private_queries_cannot_bypass_owner_filters():
    from sqlalchemy import select
    with user_scope("B"), SessionLocal() as session:
        with pytest.raises(NotFound):
            session.execute(select(db.UserProfile.__table__))


@pytest.mark.parametrize("core_kind", ["table", "column", "alias", "subquery"])
def test_mixed_orm_core_private_queries_are_rejected(core_kind):
    from app.secrets_vault import UserSecret
    table = UserSecret.__table__
    core = {"table": table, "column": table.c.user_id,
            "alias": table.alias("hidden_secrets"),
            "subquery": select(table).subquery()}[core_kind]
    with user_scope("B"), SessionLocal() as session:
        with pytest.raises(NotFound):
            session.execute(select(db.UserProfile, core))


def test_channel_rebinding_is_global_but_reads_remain_scoped():
    from app.channels import binding
    from app.data_access.channel_identity import resolve_channel_identity
    a, b, external = (uuid.uuid4().hex for _ in range(3))
    with user_scope(a):
        binding.bind_channel("s2", external, a, display_name="original")
    with user_scope(b):
        assert binding.get_binding("s2", external) is None
        binding.bind_channel("s2", external, b)
        assert binding.get_binding("s2", external)["user_id"] == b
    with user_scope(a):
        assert binding.get_binding("s2", external) == binding.get_binding("s2", "missing")
        assert not binding.unbind_channel("s2", external)
        assert resolve_channel_identity("s2", external) == b
    with system_scope():
        rows = [r for r in binding.list_bindings() if r["external_user_id"] == external]
        assert len(rows) == 1
        assert rows[0]["display_name"] == "original"


def test_feedback_export_namespaces_destination_and_contents(tmp_path, monkeypatch):
    import json
    from app.observability import feedback
    from app.agent.harness import traces
    from app.data_access.namespaces import user_directory
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.init_db()
    owners = [uuid.uuid4().hex, uuid.uuid4().hex]
    records = []
    for owner in owners:
        trace = uuid.uuid4().hex
        with system_scope(), SessionLocal.begin() as session:
            row = feedback.Feedback(user_id=owner, trace_id=trace, value=1, comment=owner)
            session.add(row)
            session.flush()
            records.append((row.id, trace))
    monkeypatch.setattr(traces, "query_traces", lambda trace_id, limit: {"traces": [
        {"tool": "test", "intent": "test", "input_summary": trace_id,
         "result_summary": trace_id, "guardrails": []}]})
    class Offline:
        enabled = False
    paths = []
    for owner, (ident, trace) in zip(owners, records):
        with system_scope():
            result = feedback.add_to_dataset(ident, client=Offline())
        with user_scope(owner):
            directory = user_directory(tmp_path / "golden_candidates")
        path = directory / (result["item_id"] + ".json")
        paths.append(path)
        payload = json.loads(path.read_text())
        assert result["stored"] == "local"
        assert payload["trace_id"] == trace
        assert payload["input"]["steps"][0]["input"] == trace
        assert payload["metadata"]["comment"] == owner
    assert paths[0].parent != paths[1].parent
    for index, path in enumerate(paths):
        other = paths[1 - index]
        assert not (path.parent / other.name).exists()
        assert owners[1 - index] not in path.read_text()
        assert records[1 - index][1] not in path.read_text()


@pytest.mark.parametrize("store", ["episodic", "summary", "schedule"])
def test_registered_tools_hide_foreign_resources_for_read_update_delete(store):
    """Exercise production registry dispatch, including forged identity arguments."""
    from app.agent.tool_registry import invoke_tool
    from app.agent.schedules import service
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with user_scope(a):
        if store in ("episodic", "summary"):
            row = invoke_tool("memory_write", {"kind": "summary" if store == "summary" else "chat",
                                               "summary": "private A", "thread_id": a})
            key, missing, id_arg = row["id"], -1, "event_id"
            calls = [("memory_get", {}), ("memory_update", {"summary": "forged"}),
                     ("memory_delete", {}), ("memory_episode_update", {"value": "forged"}),
                     ("memory_episode_delete", {})]
        else:
            row = service.mutate(a, "create", {
                "name": "private A", "job_kind": "custom_nl",
                "trigger": {"type": "cron", "expr": "0 8 * * *"}, "prompt_text": "private A",
            })
            key, missing, id_arg = row["id"], -1, "schedule_id"
            calls = [("schedule.pause", {}), ("schedule.resume", {}),
                     ("schedule.update", {"patch": {"name": "forged"}}), ("schedule.delete", {})]
    with user_scope(b):
        for name, args in calls:
            foreign = invoke_tool(name, {**args, id_arg: key, "user_id": a, "owner_id": a})
            absent = invoke_tool(name, {**args, id_arg: missing, "user_id": a, "owner_id": a})
            for outcome in (foreign, absent):
                if isinstance(outcome, dict):
                    outcome.pop("operation_id", None)
            assert foreign == absent, (name, foreign, absent)
            assert "private A" not in str(foreign)
    with user_scope(a):
        name, args = calls[0]
        own = invoke_tool(name, {**args, id_arg: key})
        if store == "schedule":
            assert own["schedule"]["id"] == key and own["schedule"]["name"] == "private A"
        else:
            assert own == row
        # Owner deletion (or deletion proposal) remains functional.
        name, args = calls[-1]
        result = invoke_tool(name, {**args, id_arg: key})
        assert not isinstance(result, dict) or "error" not in result
        if store == "schedule":
            service.mutate(a, "delete", {"id": key})


def test_profile_card_tools_forged_owner_read_update_delete():
    from app.agent.tool_registry import invoke_tool
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    for owner in (a, b):
        with user_scope(owner):
            db.save_profile(owner, {"city": owner, "dialogue_summary": owner})
    with user_scope(b):
        assert invoke_tool("profile_read", {"user_id": a})["city"] == b
        card = invoke_tool("memory_card", {"owner_id": a})
        assert a not in str(card)
        changed = invoke_tool("memory_fact_update", {"user_id": a, "fact_id": "city", "value": "changed"})
        assert "error" not in changed
        deleted = invoke_tool("memory_fact_delete", {"owner_id": a, "fact_id": "city"})
        assert "error" not in deleted
    with user_scope(a):
        profile = invoke_tool("profile_read", {})
        assert profile["city"] == a and profile["dialogue_summary"] == a


def _store_rows(model):
    """Snapshot all persisted fields under the caller's scope, including timestamps."""
    with SessionLocal() as session:
        return [{c.key: getattr(row, c.key) for c in model.__table__.columns}
                for row in session.scalars(select(model).order_by(*model.__table__.primary_key))]


def test_channel_inbound_scopes_pending_bindings_and_link_rotation(mock_text_channel):
    from app.channels import binding, conversation, gateway, links
    from app.channels.base import ChannelOption
    from app.graph import get_checkpointer
    from langgraph.checkpoint.base import empty_checkpoint
    from tests.conftest import token_from

    # Conversations have no HTTP CRUD route or registered tool. The trusted
    # inbound handler resolves the adapter's external sender, ignoring body owners.
    # Binding HTTP CRUD is admin-only; users read/unbind through this same handler.
    a, b, absent = (uuid.uuid4().hex for _ in range(3))
    channel = mock_text_channel.name

    def inbound(external, text, claimed, action=None):
        return gateway.handle_inbound({"type": "inbound", "kind": "message", "inbound": {
            "channel": channel, "external_user_id": external, "text": text,
            "user_id": claimed, "owner_id": claimed,
            "action_value": action or {},
        }}, channel)

    for owner in (a, b):
        with user_scope(owner):
            binding.bind_channel(channel, owner, owner)
    with user_scope(a):
        cp = empty_checkpoint()
        cp["channel_values"] = {"user_id": a, "draft": {"kind": "email", "title": "private A", "body": "private A"}}
        get_checkpointer().put({"configurable": {"thread_id": a, "checkpoint_ns": ""}}, cp, {}, {})
        assert inbound(a, "解绑", a)["status"] == "unbind_confirm"
        a_link = token_from(inbound(a, "隐私", a)["reply_struct"]["links"][0]["url"])
        conversation.set_pending(channel, a, kind="confirm", thread_id=a, options=[
            ChannelOption(id="confirm", label="private A", value={"command": "CONFIRM", "thread_id": a})])
        models = (binding.ChannelBinding, conversation.ChannelConversation, links.ChannelLinkToken)
        before = [_store_rows(model) for model in models]
        pending = conversation.get_pending(channel, a)

    with user_scope(b):
        # Foreign and nonexistent forged owners yield the same actual B identity.
        results = [inbound(b, "我是谁", claimed) for claimed in (a, absent)]
        assert results[0]["reply_struct"] == results[1]["reply_struct"]
        assert all(r["status"] == "already_bound" and r["outbound"]["user_id"] == b
                   for r in results)
        # An implicit confirmation reads B's pending state, never A's private menu.
        results = [inbound(b, "确认", claimed) for claimed in (a, absent)]
        assert results[0]["status"] == results[1]["status"] == "need_thread"
        assert results[0]["reply_struct"] == results[1]["reply_struct"]
        # Forged thread IDs on card actions must hit the real confirmation boundary.
        for command in ("CONFIRM", "REJECT"):
            results = [inbound(b, "", a, {"command": command, "thread_id": ident,
                       "user_id": a, "external_user_id": a})
                       for ident in (a, absent)]
            assert results[0]["status"] == results[1]["status"] == "thread_not_found"
            assert results[0]["reply_struct"] == results[1]["reply_struct"]
            assert "private A" not in str(results[0]["reply_struct"])
        # Writing and resolving B's pending menu cannot read or clear A's menu.
        for claimed in (a, absent):
            assert inbound(b, "解绑", claimed)["status"] == "unbind_confirm"
            assert conversation.get_pending(channel, b)["options"]
            assert inbound(b, "2", claimed)["status"] == "cancelled"
            assert conversation.get_pending(channel, b) == {}
        old = token_from(inbound(b, "隐私", a)["reply_struct"]["links"][0]["url"])
        new = token_from(inbound(b, "隐私", absent)["reply_struct"]["links"][0]["url"])
        assert links.peek(old, "sandbox") is None
        assert links.peek(new, "sandbox")["user_id"] == b
        assert inbound(b, "解绑", a)["status"] == "unbind_confirm"
        assert inbound(b, "1", a)["status"] == "unbound"
        assert binding.get_binding(channel, b) is None
    with user_scope(a):
        assert [_store_rows(model) for model in models] == before
        assert conversation.get_pending(channel, a) == pending
        assert links.peek(a_link, "sandbox")["user_id"] == a
        assert inbound(a, "", a, {"command": "CANCEL"})["status"] == "cancelled"
        assert conversation.get_pending(channel, a) == {}
        assert inbound(a, "解绑", a)["status"] == "unbind_confirm"
        assert inbound(a, "1", a)["status"] == "unbound"
        assert binding.get_binding(channel, a) is None


def test_http_channel_binding_admin_routes_reject_user_sessions(client, auth, monkeypatch, mock_text_channel):
    from app.channels import binding

    a, b, absent = (uuid.uuid4().hex for _ in range(3))
    channel = mock_text_channel.name
    monkeypatch.setenv("CHANNEL_ADMIN_TOKEN", uuid.uuid4().hex)
    for owner in (a, b):
        with user_scope(owner):
            binding.bind_channel(channel, owner, owner)
    with user_scope(a):
        before = _store_rows(binding.ChannelBinding)
    # These routes require an admin credential, not either user's session.
    for method in ("GET", "POST", "DELETE"):
        results = []
        for ident in (a, absent):
            params = {"channel": channel, "external_user_id": ident, "user_id": ident}
            path = "/api/channels/bindings" if method == "GET" else "/api/channels/bind"
            kwargs = {"json": {**params, "user_id": b}} if method == "POST" else {"params": params}
            response = client.request(method, path, headers=auth(b), **kwargs)
            results.append((response.status_code, response.json()))
        assert results[0] == results[1]
        assert results[0][0] == 401
    with user_scope(a):
        assert _store_rows(binding.ChannelBinding) == before
    with user_scope(b):
        assert binding.get_binding(channel, b)["user_id"] == b


def test_http_channel_links_use_capability_owner_and_hide_foreign_threads(client, auth, monkeypatch,
                                                                        mock_text_channel):
    from app.channels import binding, links
    from app.graph import get_checkpointer
    from langgraph.checkpoint.base import empty_checkpoint

    # Link routes deliberately use bearer capabilities, not the browser session.
    # A valid A link authorizes A; forging owner/external/thread fields on a B
    # link must not retarget it. There is no user-facing link delete/update API;
    # consumption is tested here and revocation via inbound rotation above.
    a, b, absent = (uuid.uuid4().hex for _ in range(3))
    channel = mock_text_channel.name

    def issue(purpose, owner, thread=""):
        return links.issue(purpose, channel=channel, external_user_id=owner,
                           user_id=owner, ttl_seconds=600, payload={"thread_id": thread})

    for owner in (a, b):
        with user_scope(owner):
            binding.bind_channel(channel, owner, owner)
            db.save_profile(owner, {"city": owner})
            cp = empty_checkpoint()
            cp["channel_values"] = {"user_id": owner, "draft": {"kind": "email", "title": owner, "body": owner}}
            get_checkpointer().put({"configurable": {"thread_id": owner, "checkpoint_ns": ""}}, cp, {}, {})
    with user_scope(a):
        issue("sandbox", a)
        issue("draft", a, a)
        models = (links.ChannelLinkToken, binding.ChannelBinding)
        before = [_store_rows(model) for model in models]
        profile_before = db.get_profile(a)
    headers = auth(b)
    with user_scope(b):
        own_draft = issue("draft", b, b)
        foreign_draft = issue("draft", b, a)
        missing_draft = issue("draft", b, absent)
    results = []
    for token in (foreign_draft, missing_draft):
        response = client.get("/api/sandbox/draft", headers=headers, params={"t": token, "user_id": a})
        results.append((response.status_code, response.json()))
    assert results[0] == results[1]
    assert results[0][0] == 404
    results = []
    for claimed in (a, absent):
        response = client.get("/api/sandbox/draft", headers=headers,
                              params={"t": own_draft, "user_id": claimed, "thread_id": claimed})
        assert response.status_code == 200
        assert response.json()["thread_id"] == b and response.json()["body"] == b
        results.append(response.json())
    assert results[0] == results[1]

    # Binding trusts only the caller's signed session (B), never body fields.
    for claimed in (a, absent):
        with user_scope(b):
            bind_token = issue("bind", b)
            sandbox_token = issue("sandbox", b)
        forged = {"user_id": claimed, "owner_id": claimed,
                  "external_user_id": claimed, "channel": "forged"}
        response = client.get("/api/channels/link", headers=headers,
                              params={"t": sandbox_token, "purpose": "sandbox", **forged})
        assert response.status_code == 200 and response.json()["channel"] == channel
        assert "user_id" not in response.json() and "payload" not in response.json()
        response = client.post("/api/channels/bind/complete", headers=headers,
                               json={"token": bind_token, **forged})
        assert response.status_code == 200 and response.json()["status"] == "bound"
        body = {"token": sandbox_token, "email": "b@example.test", **forged}
        response = client.post("/api/sandbox/submit", headers=headers, json=body)
        assert response.status_code == 200 and response.json()["status"] == "profile_updated"
        with user_scope(b):
            assert db.get_profile(b)["email"] == "b@example.test"
            assert binding.get_binding(channel, b)["user_id"] == b
            assert links.peek(bind_token, "bind") is links.peek(sandbox_token, "sandbox") is None
        replay = client.post("/api/sandbox/submit", headers=headers, json=body)
        missing = client.post("/api/sandbox/submit", headers=headers, json={**body, "token": absent})
        assert replay.status_code == missing.status_code == 410
        assert replay.json() == missing.json()
    with user_scope(a):
        assert [_store_rows(model) for model in models] == before
        assert db.get_profile(a) == profile_before


def test_http_private_store_owner_arguments(client, auth):
    """Lead-run HTTP coverage for stores without registered CRUD tools."""
    from app import secrets_vault, confirm_store
    from app.agent.proactive import store
    a, b, absent = (uuid.uuid4().hex for _ in range(3))
    with user_scope(a):
        secrets_vault.save_secret(a, "phone", {"phone": "13900001111"})
        item = store.create_item(a, "test", body="private A")
        confirm_store.record_decision(a, a, True)
    headers = auth(b)
    # Forged owners must behave identically whether that owner exists or not.
    for method, path, body in [
        ("GET", "/api/secrets", None),
        ("DELETE", "/api/secrets/phone", None),
        ("POST", "/api/secrets", {"key": "phone", "values": {"phone": "13900002222"}}),
        ("GET", "/api/proactive/pending", None),
    ]:
        responses = []
        for owner in (a, absent):
            kwargs = {"headers": headers}
            if body is None:
                kwargs["params"] = {"user_id": owner}
            else:
                kwargs["json"] = {**body, "user_id": owner}
            response = client.request(method, path, **kwargs)
            responses.append((response.status_code, response.json()))
        assert responses[0] == responses[1], path
        assert responses[0][0] == 404, path
    for ident in (item["id"], -1):
        response = client.post(f"/api/proactive/items/{ident}/dismiss", headers=headers,
                               params={"user_id": b})
        assert response.status_code == 404
        if ident == item["id"]:
            foreign = response.json()
        else:
            assert response.json() == foreign
    for thread in (a, absent):
        response = client.get(f"/api/confirm/{thread}", headers=headers, params={"user_id": b})
        assert response.status_code == 404
        if thread == a:
            foreign = response.json()
        else:
            assert response.json() == foreign
    with user_scope(a):
        assert secrets_vault.load_secret_values(a, "phone")["phone"] == "13900001111"
        assert store.item_owner(item["id"]) == a
        assert confirm_store.get_decision(a)["confirmed"] is True


@pytest.mark.parametrize("kind", ["same_table", "nested", "alias_where"])
def test_core_references_cannot_borrow_another_orm_scope(kind):
    from sqlalchemy import literal
    from sqlalchemy.orm import aliased
    table = db.UserProfile.__table__
    if kind == "same_table":
        query = select(db.UserProfile, table)
    elif kind == "nested":
        query = select(db.UserProfile).where(select(literal(1)).select_from(table).exists())
    else:
        query = select(aliased(db.UserProfile)).where(table.c.user_id == "A")
    with user_scope("B"), SessionLocal() as session:
        with pytest.raises(NotFound):
            session.execute(query)


def test_orm_projection_alias_and_nested_queries_remain_owner_filtered():
    from sqlalchemy import func, literal
    from sqlalchemy.orm import aliased
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with user_scope(a):
        db.save_profile(a, {"city": "private A"})
    with user_scope(b), SessionLocal() as session:
        alias = aliased(db.UserProfile)
        assert session.scalars(select(alias).where(alias.user_id == a)).all() == []
        assert session.scalar(select(func.count(db.UserProfile.user_id)).where(db.UserProfile.user_id == a)) == 0
        assert session.execute(select(literal(1)).where(db.UserProfile.user_id == a)).all() == []
