"""Per-admin tokens (env + DB), scopes, revocation and the admin audit log."""

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.harness import admin
from app.main import app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("HARNESS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HARNESS_ADMIN_TOKENS", raising=False)
    with TestClient(app) as c:
        yield c
    from sqlalchemy import delete

    with admin.SessionLocal() as s:
        s.execute(delete(admin.AdminToken))
        s.commit()


def _get(c, path, token=None, **params):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.get(path, params=params, headers=headers)


def test_env_tokens_named_principals_and_scopes(client, monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ADMIN_TOKENS",
        "alice:" + "A" * 24 + ":traces:read|audit:read,\nbob:" + "B" * 24 + ":traces:read",
    )
    assert _get(client, "/api/admin/traces", "A" * 24).status_code == 200
    assert _get(client, "/api/admin/traces", "B" * 24).status_code == 200
    assert _get(client, "/api/admin/audit", "B" * 24).status_code == 403
    r = _get(client, "/api/admin/audit", "A" * 24, principal="bob")
    assert r.status_code == 200
    outcomes = {e["outcome"] for e in r.json()["data"]["events"]}
    assert {"allowed", "forbidden"} <= outcomes


def test_db_tokens_create_use_revoke(client, monkeypatch):
    tok = admin.create_token("carol", "traces:read|audit:read")
    assert tok.startswith("hat_")
    listed = [t for t in admin.list_tokens() if t["name"] == "carol"]
    assert listed and tok not in json.dumps(listed)
    assert _get(client, "/api/admin/traces", tok).status_code == 200
    assert [t for t in admin.list_tokens() if t["name"] == "carol"][0]["last_used_at"]
    admin.create_token("carol-keep", "audit:read")
    assert admin.revoke_token(name="carol") == 1
    assert _get(client, "/api/admin/traces", tok).status_code == 401


def test_create_token_rejects_unknown_scope(client):
    with pytest.raises(ValueError):
        admin.create_token("dave", "traces:write")


def test_legacy_token_still_works(client, monkeypatch):
    monkeypatch.setenv("HARNESS_ADMIN_TOKEN", "L" * 32)
    assert _get(client, "/api/admin/traces", "L" * 32).status_code == 200
    r = _get(client, "/api/admin/audit", "L" * 32, principal="legacy")
    assert r.json()["data"]["events"][0]["source"] == "legacy"


def test_denied_requests_audited_without_raw_ids_or_tokens(client, monkeypatch):
    monkeypatch.setenv("HARNESS_ADMIN_TOKENS", "erin:" + "E" * 24)
    bad = "wrong-token-" + "x" * 20
    assert _get(client, "/api/admin/traces", bad, user_id="raw-user-123").status_code == 401
    assert client.get("/api/traces/raw-thread-9").status_code == 401
    data = _get(client, "/api/admin/audit", "E" * 24, outcome="denied").json()["data"]
    blob = json.dumps(data, ensure_ascii=False)
    assert data["total"] >= 2 and data["by_outcome"]["denied"] >= 2
    assert "raw-user-123" not in blob and "raw-thread-9" not in blob and bad not in blob
    assert any(e["token_fp"] for e in data["events"])


def test_disabled_without_any_token(client, monkeypatch):
    monkeypatch.setattr(admin, "_db_has_tokens", lambda: False)
    assert _get(client, "/api/admin/traces", "anything-long-enough").status_code == 503


def test_audit_write_failure_refuses_allowed_request(client, monkeypatch):
    monkeypatch.setenv("HARNESS_ADMIN_TOKENS", "frank:" + "F" * 24)

    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(admin, "write_audit", broken)
    r = _get(client, "/api/admin/traces", "F" * 24)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "admin_audit_unavailable"


def test_cli_create_list_revoke(client, capsys):
    from scripts.admin_tokens import main

    assert main(["create", "cli-user", "--scopes", "audit:read"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["token"].startswith("hat_")
    assert main(["list"]) == 0
    assert "cli-user" in capsys.readouterr().out
    assert main(["revoke", "--name", "cli-user"]) == 0
