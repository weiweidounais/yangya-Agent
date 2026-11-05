"""Stage 3: real shared references and a test-only public DB store.

HTTP tests are deliberately present for the lead's full-suite run; sandbox runs
select non-HTTP cases with -k 'not http'. No production table is reclassified.
"""
import copy
import json
import uuid
from collections import deque

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine, delete, insert, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import declarative_base, relationship

from app.data_access import IdentityRequired, system_scope, user_scope
from app.data_access import public
from app.data_access.public import PublicWriteForbidden
from app.data_access.session import make_session_factory


PublicBase = declarative_base()


class PublicReference(PublicBase):
    __tablename__ = "ac4_test_public_reference"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


public_links = Table(
    "ac4_test_public_links", PublicBase.metadata,
    Column("parent_id", ForeignKey("ac4_test_link_parent.id"), primary_key=True),
    Column("child_id", ForeignKey("ac4_test_link_child.id"), primary_key=True),
)


class LinkParent(PublicBase):
    __tablename__ = "ac4_test_link_parent"
    id = Column(Integer, primary_key=True)
    children = relationship("LinkChild", secondary=public_links)


class LinkChild(PublicBase):
    __tablename__ = "ac4_test_link_child"
    id = Column(Integer, primary_key=True)


@pytest.fixture
def public_store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'public.sqlite'}")
    PublicBase.metadata.create_all(engine)
    factory = make_session_factory(engine)
    monkeypatch.setattr(public, "PUBLIC_TABLES", public.PUBLIC_TABLES)
    with system_scope():
        public.register_public_table(PublicReference.__table__)
        with factory.begin() as session:
            session.add(PublicReference(id=1, name="shared"))
    yield factory
    engine.dispose()


def test_production_public_inventory_keeps_user_stores_private():
    from app.db import UserProfile
    from app.secrets_vault import UserSecret
    from app.data_access.session import owner_column

    assert public.PUBLIC_TABLES == frozenset()
    assert owner_column(UserProfile) is not None
    assert owner_column(UserSecret) is not None
    assert set(public.SHARED_REFERENCE_STORES) == {
        "prompt_templates", "skills", "reference_vocabularies", "tool_and_adapter_catalogs",
    }
    for private in (UserProfile, UserSecret):
        with system_scope(), pytest.raises(ValueError):
            public.register_public_table(private.__table__)
    with user_scope("A"), pytest.raises(PublicWriteForbidden):
        public.register_public_table(PublicReference.__table__)


@pytest.mark.parametrize("scope", [lambda: user_scope("A"), lambda: user_scope("B"), system_scope])
def test_public_reads_in_every_scope(public_store, scope):
    table = PublicReference.__table__
    with scope(), public_store() as session:
        assert session.get(PublicReference, 1).name == "shared"
        assert session.scalars(select(PublicReference.name)).all() == ["shared"]
        assert session.execute(select(table.c.name)).scalar_one() == "shared"
        assert session.execute(select(table.alias().c.name)).scalar_one() == "shared"


@pytest.mark.parametrize("scope", [lambda: user_scope("A"), lambda: user_scope("B"), system_scope])
def test_public_reads_allow_select_text_fragments(public_store, scope):
    with scope(), public_store() as session:
        statement = (select(PublicReference.__table__).where(text("name = :name"))
                     .order_by(text("name")))
        assert session.execute(statement, {"name": "shared"}).all() == [(1, "shared")]
        assert session.execute(statement, {"name": "absent"}).all() == []


@pytest.mark.parametrize("nested", [False, True])
def test_opaque_text_select_is_not_verified(public_store, nested):
    statement = text("DELETE FROM ac4_test_public_reference RETURNING id").columns(id=Integer)
    if nested:
        statement = select(statement.cte("removed"))
    with user_scope("A"), public_store() as session, pytest.raises(PublicWriteForbidden):
        session.execute(statement)


@pytest.mark.parametrize("operation", ["union", "intersect", "except_"])
def test_public_compound_reads_allow_text_fragments(public_store, operation):
    table = PublicReference.__table__
    statement = getattr(select(table.c.name), operation)(select(table.c.name)).order_by(text("name"))
    with user_scope("A"), public_store() as session:
        expected = [] if operation == "except_" else ["shared"]
        assert session.scalars(statement).all() == expected
        with pytest.raises(PublicWriteForbidden):
            session.execute(text("DELETE FROM ac4_test_public_reference"))
        assert session.scalars(select(table.c.name)).all() == ["shared"]


def test_opaque_text_select_in_compound_is_not_verified(public_store):
    statement = select(PublicReference.name).union(
        text("DELETE FROM ac4_test_public_reference RETURNING name").columns(name=String)
    ).order_by(text("name"))
    with user_scope("A"), public_store() as session, pytest.raises(PublicWriteForbidden):
        session.execute(statement)


@pytest.fixture
def public_association(public_store):
    with system_scope(), public_store.begin() as session:
        public.register_public_table(public_links)
        session.add_all([LinkParent(id=1, children=[LinkChild(id=1)]), LinkChild(id=2)])
    return public_store


@pytest.mark.parametrize("operation", ["insert", "delete", "delete_parent"])
@pytest.mark.parametrize("system", [False, True])
def test_public_secondary_flush_requires_system_scope(public_association, operation, system):
    with public_association() as session:
        # Reuse a connection opened under system authority to check the scope
        # at execution time, rather than retaining the authority at checkout.
        with system_scope():
            parent = session.get(LinkParent, 1)
            child = session.get(LinkChild, 2)
            assert [row.id for row in parent.children] == [1]
        with system_scope() if system else user_scope("A"):
            if operation == "insert":
                parent.children.append(child)
            elif operation == "delete":
                parent.children.clear()
            else:
                session.delete(parent)
            if system:
                session.commit()
            else:
                with pytest.raises(PublicWriteForbidden):
                    session.flush()
                session.rollback()
    with user_scope("B"), public_association() as session:
        expected = [(1, 1), (1, 2)] if operation == "insert" else []
        assert session.execute(select(public_links).order_by(public_links.c.child_id)).all() == (
            expected if system else [(1, 1)])
        assert (session.get(LinkParent, 1) is None) == (system and operation == "delete_parent")


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("system", [False, True])
def test_session_connection_public_writes_require_system_scope(public_store, raw, system):
    with system_scope() if system else user_scope("A"), public_store() as session:
        connection = session.connection()

        def write():
            if raw:
                connection.exec_driver_sql("UPDATE ac4_test_public_reference SET name = 'changed'")
            else:
                connection.execute(update(PublicReference.__table__).values(name="changed"))

        if system:
            write()
            session.commit()
        else:
            with pytest.raises(PublicWriteForbidden):
                write()
            session.rollback()
    with user_scope("B"), public_store() as session:
        assert session.get(PublicReference, 1).name == ("changed" if system else "shared")


WRITE_CASES = (
    "orm_insert", "orm_update", "orm_delete", "orm_merge", "orm_bulk_insert",
    "orm_bulk_update", "orm_bulk_delete", "core_insert", "core_update", "core_delete",
    "core_alias_update", "core_alias_delete", "upsert", "insert_from_select",
    "executemany", "legacy_insert", "legacy_update", "legacy_save", "legacy_save_update",
    "text_insert", "text_update", "text_delete", "cte_write",
)


def write_public(session, kind):
    table = PublicReference.__table__
    if kind == "orm_insert":
        session.add(PublicReference(id=2, name="changed"))
    elif kind in {"orm_update", "orm_delete", "legacy_save_update"}:
        row = session.get(PublicReference, 1)
        if kind == "orm_delete":
            session.delete(row)
        else:
            row.name = "changed"
            if kind == "legacy_save_update":
                session.bulk_save_objects([row])
    elif kind == "orm_merge":
        session.merge(PublicReference(id=1, name="changed"))
    elif kind == "legacy_insert":
        session.bulk_insert_mappings(PublicReference, [{"id": 2, "name": "changed"}])
    elif kind == "legacy_update":
        session.bulk_update_mappings(PublicReference, [{"id": 1, "name": "changed"}])
    elif kind == "legacy_save":
        session.bulk_save_objects([PublicReference(id=2, name="changed")])
    elif kind == "orm_bulk_insert":
        session.execute(insert(PublicReference), [{"id": 2, "name": "changed"}])
    elif kind == "orm_bulk_update":
        session.query(PublicReference).update({"name": "changed"})
    elif kind == "orm_bulk_delete":
        session.query(PublicReference).delete()
    elif kind == "core_insert":
        session.execute(insert(table).values(id=2, name="changed").returning(table.c.id))
    elif kind in {"core_update", "core_alias_update"}:
        target = table.alias() if "alias" in kind else table
        session.execute(update(target).values(name="changed"))
    elif kind in {"core_delete", "core_alias_delete"}:
        session.execute(delete(table.alias() if "alias" in kind else table))
    elif kind == "upsert":
        session.execute(sqlite_insert(table).values(id=1, name="changed").on_conflict_do_update(
            index_elements=[table.c.id], set_={"name": "changed"}))
    elif kind == "insert_from_select":
        session.execute(insert(table).from_select(["id", "name"], select(table.c.id + 1, table.c.name)))
    elif kind == "executemany":
        session.execute(insert(table), [{"id": 2, "name": "changed"}, {"id": 3, "name": "changed"}])
    elif kind == "cte_write":
        # Rejected before execution, even on dialects without writable CTEs.
        session.execute(select(table).add_cte(delete(table).cte("removed")))
    else:
        sql = {"text_insert": "INSERT INTO ac4_test_public_reference VALUES (2, 'changed')",
               "text_update": "UPDATE ac4_test_public_reference SET name = 'changed'",
               "text_delete": "DELETE FROM ac4_test_public_reference"}[kind]
        session.execute(text(sql))
    session.flush()


@pytest.mark.parametrize("kind", WRITE_CASES)
def test_user_public_writes_rejected(public_store, kind):
    with user_scope("A"), public_store() as session:
        with pytest.raises(PublicWriteForbidden):
            write_public(session, kind)
        session.rollback()
    with user_scope("B"), public_store() as session:
        assert session.execute(select(PublicReference.id, PublicReference.name)).all() == [(1, "shared")]


@pytest.mark.parametrize("kind", [k for k in WRITE_CASES if k != "cte_write"])
def test_system_public_writes_allowed(public_store, kind):
    with system_scope(), public_store.begin() as session:
        write_public(session, kind)
    with user_scope("A"), public_store() as session:
        assert session.execute(select(PublicReference.id, PublicReference.name)).all() != [(1, "shared")]


def test_no_identity_cannot_write_public(public_store):
    with public_store() as session, pytest.raises(IdentityRequired):
        write_public(session, "core_insert")


@pytest.mark.parametrize("schema", [None, "main"])
def test_reflected_core_table_keeps_public_policy(public_store, schema):
    with user_scope("A"), public_store() as session:
        reflected = Table(PublicReference.__tablename__, MetaData(), schema=schema,
                          autoload_with=session.get_bind())
        session.execute(text("BEGIN IMMEDIATE"))
        assert session.execute(select(reflected.c.name)).scalar_one() == "shared"
        with pytest.raises(PublicWriteForbidden):
            session.execute(update(reflected).values(name="changed"))


def test_public_write_authority_is_checked_at_flush(public_store):
    with public_store() as session:
        with system_scope():
            session.add(PublicReference(id=2, name="pending"))
        with user_scope("A"), pytest.raises(PublicWriteForbidden):
            session.commit()
        session.rollback()


def test_registered_tool_cannot_write_public(public_store, monkeypatch):
    from app.agent import tool_registry

    tool_registry.ensure_defaults()
    monkeypatch.setattr(tool_registry, "_REGISTRY", dict(tool_registry._REGISTRY))

    def attempt():
        with public_store.begin() as session:
            write_public(session, "core_insert")
        return {"ok": True}

    tool_registry.register("ac4_test_public_write", attempt, risk_level="self_write")
    with user_scope("A"):
        result = tool_registry.invoke_tool("ac4_test_public_write", {})
    assert result["ok"] is False
    assert result["error"]["code"] == "public_write_forbidden"
    with system_scope(), public_store() as session:
        assert session.scalars(select(PublicReference.name)).all() == ["shared"]


def test_http_public_write_rejected(public_store, client, auth):
    from app.main import app

    def attempt():
        with public_store.begin() as session:
            write_public(session, "orm_insert")
        return {"ok": True}

    app.add_api_route("/api/ac4-test-public-write", attempt, methods=["POST"])
    route = app.routes[-1]
    try:
        response = client.post("/api/ac4-test-public-write", headers=auth("A"))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "public_write_forbidden"
        with system_scope(), public_store() as session:
            assert session.scalars(select(PublicReference.name)).all() == ["shared"]
    finally:
        app.router.routes.remove(route)


def test_chat_leaves_shared_stores_unchanged(public_store, uid, fake_llm):
    from app import db, prompts
    from app.agent import skills, tool_registry
    from app.agent.harness.leak import registered_prompts
    from app.graph import run_action

    tool_registry.ensure_defaults()
    reg = skills.registry()
    for item in reg.catalog():
        reg.load(item.name, item.version)

    def snapshot():
        return copy.deepcopy({
            "skills": reg.catalog(),
            "prompts": registered_prompts(), "versions": prompts._loaded,
            "tools": tool_registry.list_tools(),
        })

    before = snapshot()
    place = "星穹独角兽咖啡馆"
    out = run_action({"action": "chat", "user_id": uid,
                      "message": f"记住我常去的{place}简称星角，下周二提醒我去"})
    assert out["state"]["messages"]
    assert snapshot() == before
    assert place not in repr(before)
    with public_store() as session:
        assert session.scalars(select(PublicReference.name)).all() == ["shared"]
    other = uuid.uuid4().hex
    with user_scope(other):
        assert db.get_profile(other) == {}


def test_user_budget_aggregates_do_not_include_other_users(uid):
    from app.agent import cost_ledger

    other = uuid.uuid4().hex
    with user_scope(other):
        cost_ledger.record_spend(other, 50)
    cost_ledger.record_spend(uid, 0.25)
    assert cost_ledger.usage(uid) == {"today": 0.25, "month": 0.25}
    with user_scope(other):
        assert cost_ledger.usage(other) == {"today": 50, "month": 50}


def test_usage_response_deidentifies_other_users(uid, monkeypatch):
    from app.gateway import llm_gateway, usage
    from app.gateway.config import ModelSpec
    from app.main import llm_usage

    monkeypatch.setattr(usage, "_RING", deque(maxlen=500))
    monkeypatch.setattr(usage, "_totals", {})
    spec = ModelSpec(id="test-model", model="test-model", protocol="openai", base_url="https://example.invalid")
    with user_scope("foreign-user"):
        usage.record(request_id="foreign-request", task="Foreign name and private chat",
                     tier="fast", spec=spec, attempt=1, ok=False, latency_ms=10,
                     error="foreign-user: Foreign name and private chat")
    data = llm_usage(limit=50).data
    assert data["totals"]["test-model"]["calls"] == 1
    assert data["totals"]["test-model"]["errors"] == 1
    assert data["recent"][0]["task"] == "other"
    assert "foreign" not in json.dumps(data).lower()
    assert "private chat" not in json.dumps(data)
    assert llm_usage(limit=0).data["recent"] == []
    health_usage = llm_gateway.gateway_info()["usage"]
    assert health_usage == data["totals"]
    assert "foreign" not in json.dumps(health_usage).lower()


def test_channel_status_deidentifies_inbound_errors(monkeypatch):
    from app.channels import feishu_ws

    monkeypatch.setattr(feishu_ws, "_external_ws_alive", lambda: False)
    monkeypatch.setattr(feishu_ws, "_state", {
        **feishu_ws._state, "last_error": "foreign-user: Foreign name and private content",
        "events_ok": 2, "events_err": 1,
    })
    status = feishu_ws.status()
    assert status["events_ok"] == 2 and status["events_err"] == 1
    assert status["last_error"] == "channel_error"
    assert "foreign" not in json.dumps(status).lower()


def test_proactive_tick_and_status_expose_counts_only(uid, monkeypatch):
    from app.agent import proactive
    from app.agent.proactive import scheduler
    from app.main import proactive_status_api, proactive_tick

    tick = {"ok": True, "users": 2, "elapsed_ms": 10,
            "schedules": [{"user_id": "foreign-user", "text": "Foreign private content"},
                          {"user_id": "foreign-user", "name": "Foreign name"}]}
    monkeypatch.setattr(proactive, "trigger_tick_now", lambda: tick)
    monkeypatch.setattr(scheduler, "_last_tick", tick)
    data = proactive_tick().data
    assert data == {"ok": True, "users": 2, "elapsed_ms": 10, "schedules": 2}
    status = proactive_status_api().data
    assert status["last_tick"]["users"] == 2
    assert "foreign" not in json.dumps(status).lower()


@pytest.mark.parametrize("path", ["/api/admin/traces", "/api/traces/{thread_id}",
                                  "/api/admin/feedback", "/api/admin/audit",
                                  "/api/admin/feedback/{feedback_id}/to-dataset",
                                  "/api/proactive/tick"])
def test_cross_user_reports_require_admin_auth(path, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.agent.harness import admin
    from app.main import app

    route = next(route for route in app.routes if getattr(route, "path", None) == path)
    guards = [dep.call for dep in route.dependant.dependencies
              if dep.call.__module__ == admin.__name__]
    assert len(guards) == 1
    # Exercise the actual route dependency without starting TestClient.
    monkeypatch.setattr(admin, "admin_configured", lambda: True)
    monkeypatch.setattr(admin, "resolve", lambda supplied: None)
    request = Request({"type": "http", "method": "GET", "path": path,
                       "query_string": b"", "headers": []})
    with user_scope("ordinary-user"), pytest.raises(HTTPException) as exc:
        guards[0](request)
    assert exc.value.status_code == 401
