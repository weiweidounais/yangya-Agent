"""Synthetic Phase-1 tests: no network, live service or TestClient threads."""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from app import db
from app.admin import users
from app.admin.models import AdminChangeLog, AdminUserFlag
from app.agent.harness import admin
from app.data_access import system_scope
from app.data_access.session import make_session_factory


@pytest.fixture(autouse=True)
def isolated_admin(tmp_path, monkeypatch):
    for name in ("ADMIN_TOKEN", "HARNESS_ADMIN_TOKEN", "HARNESS_ADMIN_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'synthetic.sqlite'}")
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db, "SessionLocal", make_session_factory(engine))
    db.init_db()
    yield
    engine.dispose()


def request(path="/api/admin/users", method="GET", headers=None, query=b""):
    return Request({"type": "http", "method": method, "path": path, "query_string": query,
                    "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
                    "client": ("127.0.0.1", 1234), "scheme": "http", "server": ("synthetic", 80)})


def owner(monkeypatch):
    # Deliberately synthetic credential, generated per test; never a deployment value.
    import secrets
    token = secrets.token_hex(24)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    return admin.require_scope("")(request(headers={"X-Admin-Token": token}))


def seed_user():
    with system_scope(), db.SessionLocal.begin() as s:
        s.add(db.UserProfile(user_id="synthetic-user", profile_json=json.dumps({
            "contact": {"name": "合成测试用户"}, "skills": ["Python"],
            "known_facts": ["synthetic private memory"], "legacy_blob": "private blob",
        })))


def test_disabled_bad_user_jwt_and_scope_are_audited(monkeypatch):
    from app.auth import mint_user_token
    with pytest.raises(HTTPException) as exc:
        admin.require_scope("users:read")(request())
    assert exc.value.status_code == 503
    owner(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        admin.require_scope("users:read")(request(headers={"Authorization": "Bearer " + mint_user_token("synthetic")}))
    assert exc.value.status_code == 401
    token = admin.create_token("readonly", "users:read")
    assert admin.require_scope("users:read")(request(headers={"Authorization": "Bearer " + token})).name == "readonly"
    with pytest.raises(HTTPException) as exc:
        admin.require_scope("users:write")(request(method="POST", headers={"X-Admin-Token": token}))
    assert exc.value.status_code == 403
    outcomes = {e["outcome"] for e in admin.query_audit()["events"]}
    assert {"disabled", "denied", "allowed", "forbidden"} <= outcomes


@pytest.mark.parametrize("env_name", ["ADMIN_TOKEN", "HARNESS_ADMIN_TOKEN", "HARNESS_ADMIN_TOKENS"])
def test_token_sources_and_all_new_scopes(monkeypatch, env_name):
    import secrets
    token = secrets.token_hex(24)
    monkeypatch.setenv(env_name, "operator:" + token if env_name == "HARNESS_ADMIN_TOKENS" else token)
    for scope in admin.ALL_SCOPES:
        assert admin.require_scope(scope)(request(headers={"Authorization": "Bearer " + token})).allows(scope)


def test_users_search_redaction_and_disable(monkeypatch):
    principal = owner(monkeypatch)
    seed_user()
    assert users.list_users(q="合成")["total"] == 1
    assert users.list_users(q="synthetic-")["items"][0]["display_name"] == "合成测试用户"
    assert users.list_users(q="合成", offset=1)["items"] == []
    assert "private memory" not in json.dumps(users.detail("synthetic-user"), default=str)
    assert "private memory" in json.dumps(users.detail("synthetic-user", True), default=str)
    assert "legacy_blob" not in users.detail("synthetic-user")["profile"]
    users.set_disabled("synthetic-user", True, principal)
    assert users.list_users(disabled=True)["total"] == 1
    with pytest.raises(HTTPException) as exc:
        users.check_enabled("synthetic-user")
    assert exc.value.status_code == 403
    users.set_disabled("synthetic-user", False, principal)
    users.check_enabled("synthetic-user")
    with db.SessionLocal() as s:
        changes = s.scalars(select(AdminChangeLog)).all()
        assert len(changes) == 2 and all(c.before_hash != c.after_hash for c in changes)
        assert all(c.principal == principal.name for c in changes)


@pytest.mark.parametrize("path", ["/api/chat", "/api/profile"])
def test_disabled_business_middleware_blocks_before_handler(monkeypatch, path):
    from app.auth import mint_user_token
    from app.session_identity import SessionIdentityMiddleware
    import starlette.concurrency
    seed_user()
    users.set_disabled("synthetic-user", True, owner(monkeypatch))
    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(starlette.concurrency, "run_in_threadpool", inline)
    reached, sent = [], []
    async def business(scope, receive, send):
        reached.append(True)
    async def receive():
        return {"type": "http.request", "body": b"{}"}
    async def send(message):
        sent.append(message)
    req = request(path, "POST", {"Authorization": "Bearer " + mint_user_token("synthetic-user")})
    asyncio.run(SessionIdentityMiddleware(business)(req.scope, receive, send))
    assert not reached
    assert sent[0]["status"] == 403 and b"user_disabled" in sent[1]["body"]


def test_reveal_query_is_audited_without_raw_user_id(monkeypatch):
    import os
    owner(monkeypatch)
    req = request("/api/admin/users/synthetic-user", headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]}, query=b"reveal=1")
    admin.require_scope("users:read")(req)
    log = admin.query_audit()["events"][0]
    assert "reveal=1" in log["query"] and "synthetic-user" not in log["path"]


@pytest.fixture
def api(monkeypatch):
    import anyio.to_thread
    import httpx
    from app.main import app

    # Exercise actual ASGI routes without TestClient's sandbox thread hang.
    async def inline(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline)

    def call(method, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as c:
                return await c.request(method, path, **kwargs)
        return asyncio.run(run())

    return call


def test_http_auth_all_sources_scopes_and_unknown_paths(api, monkeypatch):
    import secrets
    from app.auth import mint_user_token

    for path in ("/api/admin/me", "/api/admin/users", "/api/admin/audit", "/api/admin/not-present"):
        assert api("GET", path).status_code == 503
    legacy, alias = secrets.token_hex(24), secrets.token_hex(24)
    monkeypatch.setenv("HARNESS_ADMIN_TOKEN", legacy)
    monkeypatch.setenv("ADMIN_TOKEN", alias)
    for token in (legacy, alias):
        response = api("GET", "/api/admin/me", headers={"X-Admin-Token": token})
        assert response.status_code == 200
        assert response.json()["scopes"] == ["*"]
    assert api("GET", "/api/admin/users").status_code == 401
    assert api("GET", "/api/admin/users", headers={"Authorization": "Bearer " + mint_user_token("synthetic")}).status_code == 401
    token = admin.create_token("reader", "users:read")
    headers = {"Authorization": "Bearer " + token}
    assert api("GET", "/api/admin/me", headers=headers).json()["principal"] == "reader"
    assert api("GET", "/api/admin/users", headers=headers).status_code == 200
    assert api("GET", "/api/admin/audit", headers=headers).status_code == 403
    assert api("POST", "/api/admin/users/synthetic-user/disable", headers=headers).status_code == 403
    assert api("GET", "/api/admin/routes", headers=headers).status_code == 404
    assert api("GET", "/api/admin/prompts", headers=headers).status_code == 404
    admin.revoke_token(name="reader")
    assert api("GET", "/api/admin/me", headers=headers).status_code == 401


def test_http_users_detail_reveal_and_state_transitions(api, monkeypatch):
    import os
    from app.auth import mint_user_token
    from app.data_access.memory_facts import MemoryFact

    owner(monkeypatch)
    seed_user()
    with system_scope(), db.SessionLocal.begin() as s:
        s.add(db.UserProfile(user_id="synthetic-second", profile_json=json.dumps({"name": "另一个合成用户"})))
        row = s.get(db.UserProfile, "synthetic-user")
        profile = json.loads(row.profile_json)
        profile["preferences"] = {"directions": ["后端"], "api_key": "synthetic-private-value"}
        profile["contact"]["password"] = "synthetic-private-value"
        row.profile_json = json.dumps(profile)
        s.add(MemoryFact(user_id="synthetic-user", text="synthetic vector fact", embedding=b"\0" * 4096, embed_model="synthetic"))
    headers = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
    response = api("GET", "/api/admin/users?q=SYnthetic&limit=1&offset=1", headers=headers)
    assert response.status_code == 200 and response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
    assert api("GET", "/api/admin/users?q=另一个", headers=headers).json()["total"] == 1
    assert api("GET", "/api/admin/users?q=private%20note", headers=headers).json()["total"] == 0
    detail = api("GET", "/api/admin/users/synthetic-user", headers=headers).json()
    assert not detail["revealed"] and len(detail["memory_facts"]) == 2
    assert all(set(f["value"]) == {"sha", "chars"} for f in detail["memory_facts"])
    assert "synthetic-private-value" not in json.dumps(detail)
    assert detail["profile"]["preferences"] == {"directions": ["后端"]}
    assert detail["usage"] == {"available": False}
    revealed = api("GET", "/api/admin/users/synthetic-user?reveal=1", headers=headers)
    assert revealed.json()["revealed"] and "synthetic vector fact" in revealed.text
    assert "synthetic-private-value" not in revealed.text
    logs = api("GET", "/api/admin/audit", headers=headers).json()["data"]["events"]
    assert any("reveal=1" in log["query"] and log["scope"] == "users:read" for log in logs)
    assert api("GET", "/api/admin/users/missing", headers=headers).status_code == 404
    assert api("POST", "/api/admin/users/missing/disable", headers=headers).status_code == 404
    assert api("GET", "/api/admin/users?limit=0", headers=headers).status_code == 422

    user_headers = {"Authorization": "Bearer " + mint_user_token("synthetic-user")}
    # A malformed business body proves rejection happens before handler parsing.
    for action, expected in (("disable", 403), ("disable", 403), ("enable", 422)):
        assert api("POST", f"/api/admin/users/synthetic-user/{action}", headers=headers).status_code == 200
        for path in ("/api/chat", "/api/profile"):
            response = api("POST", path, headers=user_headers, content="{", extensions={})
            assert response.status_code == expected
    assert api("GET", "/api/admin/users?disabled=true", headers=headers).json()["total"] == 0
    changes = api("GET", "/api/admin/changes", headers=headers).json()["items"]
    assert len(changes) == 2  # Repeated disable is idempotent.
    assert all(c["entity_type"] == "user" and c["entity_id"] == "synthetic-user" for c in changes)
    assert changes[0]["before_hash"] == changes[1]["after_hash"]


@pytest.mark.parametrize("key", ["access_key", "private_key", "accessKey", "privateKey", "access-key", "private.key"])
@pytest.mark.parametrize("reveal", [False, True])
def test_http_detail_removes_opaque_credentials_recursively(api, monkeypatch, key, reveal):
    import os

    owner(monkeypatch)
    seed_user()
    opaque = "synthetic-opaque-value-without-credential-pattern"
    payload = {key: opaque, "nested": [{key: opaque, "city": "上海"}]}
    with system_scope(), db.SessionLocal.begin() as s:
        row = s.get(db.UserProfile, "synthetic-user")
        profile = json.loads(row.profile_json)
        profile["contact"].update(payload)
        profile["preferences"] = payload
        profile["known_facts"] = [payload]
        row.profile_json = json.dumps(profile)

    response = api("GET", f"/api/admin/users/synthetic-user?reveal={int(reveal)}",
                   headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]})
    assert response.status_code == 200
    assert opaque not in response.text and key not in response.text
    detail = response.json()
    assert detail["profile"]["contact"] == {"name": "合成测试用户", "nested": [{"city": "上海"}]}
    assert detail["profile"]["preferences"] == {"nested": [{"city": "上海"}]}
    if reveal:
        assert detail["memory_facts"][0]["value"] == [{"nested": [{"city": "上海"}]}]
    else:
        assert set(detail["memory_facts"][0]["value"]) == {"sha", "chars"}


@pytest.mark.parametrize("failure", ["audit", "insert", "commit"])
@pytest.mark.parametrize("action", ["disable", "enable"])
def test_mutation_failure_rolls_back_flags_and_journal(api, monkeypatch, failure, action):
    import os
    principal = owner(monkeypatch)
    seed_user()
    if action == "enable":
        users.set_disabled("synthetic-user", True, principal)

    def unavailable(*args, **kwargs):
        raise OperationalError("synthetic", {}, Exception("unavailable"))

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO admin_change_log"):
            if failure == "insert":
                unavailable()
            conn.info["synthetic_pending"] = True

    def before_commit(conn):
        if conn.info.pop("synthetic_pending", False) and failure == "commit":
            unavailable()

    if failure == "audit":
        monkeypatch.setattr(admin, "write_audit", unavailable)
    event.listen(db.ENGINE, "before_cursor_execute", before_execute)
    event.listen(db.ENGINE, "commit", before_commit)
    try:
        response = api("POST", f"/api/admin/users/synthetic-user/{action}",
                       headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]})
    finally:
        event.remove(db.ENGINE, "before_cursor_execute", before_execute)
        event.remove(db.ENGINE, "commit", before_commit)
    assert response.status_code == 503
    assert users.detail("synthetic-user")["disabled"] == (action == "enable")
    with db.SessionLocal() as s:
        assert len(s.scalars(select(AdminChangeLog)).all()) == (1 if action == "enable" else 0)


def test_reveal_requires_writable_access_audit(api, monkeypatch):
    import os
    owner(monkeypatch)
    seed_user()
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(admin, "write_audit", broken)
    response = api("GET", "/api/admin/users/synthetic-user?reveal=1",
                   headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]})
    assert response.status_code == 503 and "private memory" not in response.text


def test_spa_shell_and_scoped_api_use_no_production_routes(api, monkeypatch):
    assert api("GET", "/admin").status_code == 503
    owner(monkeypatch)
    for path in ("/admin", "/admin/", "/admin/users", "/admin/audit"):
        response = api("GET", path)
        assert response.status_code == 200 and 'lang="zh-CN"' in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    js = api("GET", "/admin/admin.js").text
    assert "sessionStorage" in js and "localStorage" not in js
    assert "Authorization:'Bearer '" in js and "下一期" in js
    assert "innerHTML" not in js
    assert "routes/publish" not in js and "prompts/publish" not in js
    assert api("GET", "/admin/admin.css").status_code == 200
    assert api("GET", "/admin/not-present").status_code == 404


@pytest.mark.parametrize("journal_failure", [None, "insert", "commit"])
@pytest.mark.parametrize("destination", ["local", "langfuse"])
def test_feedback_export_journal_precedes_side_effects(monkeypatch, tmp_path, journal_failure, destination):
    import secrets
    from types import SimpleNamespace
    import anyio.to_thread
    import httpx
    from app.main import app
    from app.observability import feedback, config, langfuse_client
    from app.admin.store import sha
    db.init_db()
    token = secrets.token_hex(24)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    monkeypatch.setattr(config, "langfuse_config", lambda: SimpleNamespace(
        enabled=destination == "langfuse", feedback_dataset="synthetic"))
    monkeypatch.setattr(feedback, "_dataset_input", lambda trace_id: {"steps": []})
    export_calls = []

    def exported(*args, **kwargs):
        # Read on a separate session: the journal must already be durable.
        with db.SessionLocal() as s:
            assert s.scalar(select(AdminChangeLog).where(AdminChangeLog.entity_type == "feedback_dataset"))
        export_calls.append(True)

    def candidates_dir():
        exported()
        return tmp_path

    monkeypatch.setattr(langfuse_client, "LangfuseClient", lambda cfg: SimpleNamespace(
        enabled=True, create_dataset=exported, create_dataset_item=exported))
    monkeypatch.setattr(feedback, "_candidates_dir", candidates_dir)
    with system_scope(), db.SessionLocal.begin() as s:
        row = feedback.Feedback(user_id="synthetic", trace_id="a" * 32, value=1)
        s.add(row)
        s.flush()
        feedback_id = row.id

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO admin_change_log"):
            if journal_failure == "insert":
                raise OperationalError("synthetic", {}, Exception("unavailable"))
            conn.info["synthetic_journal_pending"] = True

    def before_commit(conn):
        if conn.info.pop("synthetic_journal_pending", False) and journal_failure == "commit":
            raise OperationalError("synthetic", {}, Exception("unavailable"))

    async def inline(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline)
    event.listen(db.ENGINE, "before_cursor_execute", before_execute)
    event.listen(db.ENGINE, "commit", before_commit)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            return await client.post(f"/api/admin/feedback/{feedback_id}/to-dataset",
                                     headers={"X-Admin-Token": token}, json={"dataset": "synthetic"})

    try:
        response = asyncio.run(exercise())
    finally:
        event.remove(db.ENGINE, "before_cursor_execute", before_execute)
        event.remove(db.ENGINE, "commit", before_commit)
    with system_scope(), db.SessionLocal() as s:
        row = s.get(feedback.Feedback, feedback_id)
        logs = s.scalars(select(AdminChangeLog).where(AdminChangeLog.entity_type == "feedback_dataset")).all()
        if journal_failure:
            assert response.status_code == 503
            assert not export_calls and not list(tmp_path.glob("fb-*.json"))
            assert row.dataset == "" and row.dataset_item_id == "" and not logs
        else:
            assert response.status_code == 200
            assert len(export_calls) == (1 if destination == "local" else 2)
            assert len(list(tmp_path.glob("fb-*.json"))) == (1 if destination == "local" else 0)
            assert row.dataset == ("local:synthetic" if destination == "local" else "synthetic")
            assert row.dataset_item_id
            assert len(logs) == 1 and logs[0].principal == "legacy"
            assert logs[0].entity_id == str(feedback_id)
            assert logs[0].before_hash == sha({"dataset": "", "dataset_item_id": ""})
            assert logs[0].after_hash == sha({"dataset": row.dataset, "dataset_item_id": row.dataset_item_id})



def test_init_db_idempotently_creates_only_slice_tables():
    from sqlalchemy import inspect
    db.init_db()
    tables = set(inspect(db.ENGINE).get_table_names())
    assert {"admin_user_flags", "admin_change_log"} <= tables
    assert not tables & {"admin_route_versions", "admin_route_pins", "admin_prompt_versions", "admin_prompt_pins"}


def test_malformed_mutation_still_requires_auth_and_audit(api, monkeypatch):
    import os
    assert api("POST", "/api/admin/feedback/1/to-dataset", content="{",
               headers={"Content-Type": "application/json"}).status_code == 503
    owner(monkeypatch)
    response = api("POST", "/api/admin/feedback/1/to-dataset", content="{", headers={
        "Content-Type": "application/json", "X-Admin-Token": os.environ["ADMIN_TOKEN"]})
    assert response.status_code == 422
    events = admin.query_audit()["events"]
    assert sum(e["path"] == "/api/admin/feedback/1/to-dataset" for e in events) == 2


def test_spa_synthetic_interactions():
    from pathlib import Path
    import subprocess
    root = Path(__file__).resolve().parents[2]
    subprocess.run(["node", str(Path(__file__).with_name("admin_slice_a_ui.mjs"))],
                   cwd=root, check=True, capture_output=True, text=True, timeout=10)
