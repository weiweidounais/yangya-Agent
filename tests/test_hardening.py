"""HTTP hardening: headers, CORS default, rate limiting, data export, backup."""

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from app import db, ratelimit
from app.agent.memory import episodic
from app.data_access import user_scope
from app.sports import store as sport_store

ROOT = Path(__file__).resolve().parents[1]


def test_security_headers_on_pages_and_api(client):
    for path in ("/health", "/", "/api/capabilities"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["referrer-policy"] == "no-referrer"
        assert res.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
        assert "geolocation=()" in res.headers["permissions-policy"]


def test_cors_is_not_wildcard_by_default(client):
    allowed = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    ok = client.get("/api/capabilities", headers={"Origin": allowed})
    assert ok.headers.get("access-control-allow-origin") == allowed
    blocked = client.get("/api/capabilities", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in blocked.headers
    assert "*" not in (blocked.headers.get("access-control-allow-origin") or "")


def test_rate_limit_blocks_per_ip_and_returns_retry_after(client, auth, monkeypatch):
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "5")
    monkeypatch.setenv("API_RATE_LIMIT_USER_PER_MINUTE", "100")
    ratelimit.reset()
    headers = auth("u-rate-test")
    codes = [client.get("/api/capabilities", headers=headers).status_code for _ in range(6)]
    assert codes == [200, 200, 200, 200, 200, 429]
    denied = client.get("/api/capabilities", headers=headers)
    assert denied.status_code == 429
    assert denied.json()["detail"]["code"] == "rate_limited"
    assert denied.headers["retry-after"].isdigit()
    assert denied.headers["x-content-type-options"] == "nosniff"
    ratelimit.reset()


def test_rate_limit_does_not_touch_health(client, monkeypatch):
    monkeypatch.setenv("API_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "1")
    ratelimit.reset()
    assert [client.get("/health").status_code for _ in range(4)] == [200, 200, 200, 200]
    ratelimit.reset()


def test_rate_limit_is_off_by_default(client, monkeypatch):
    monkeypatch.delenv("API_RATE_LIMIT_ENABLED", raising=False)
    ratelimit.reset()
    assert [client.get("/api/capabilities").status_code for _ in range(6)] == [200] * 6


def test_export_returns_owner_data_without_secrets(client, auth, uid):
    with user_scope(uid):
        sport_store.log_workout(uid, sport="跑步", duration_min=30, distance_km=5)
        episodic.write_event(uid, "meal", "燕麦 约 350 kcal",
                             payload={"name": "燕麦", "kcal": 350})
        db.merge_and_save(uid, {"privacy_sandbox": ["very-private-value"], "name": "导出用户"})
    res = client.get("/api/export", headers=auth(uid))
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    assert "attachment" in res.headers["content-disposition"]
    data = res.json()
    assert data["user_id"] == uid
    assert data["workouts"] and data["meals"]
    assert data["profile"]["name"] == "导出用户"
    sandbox = data["profile"].get("privacy_sandbox")
    assert isinstance(sandbox, dict) and sandbox.get("values") == "hidden"
    assert "very-private-value" not in res.text
    assert "saved_secret_labels" in data


def test_export_is_owner_scoped(client, auth, uid):
    res = client.get(f"/api/export?user_id=u-another-user", headers=auth(uid))
    assert res.status_code == 404


def test_backup_script_creates_archive_and_keeps_last(tmp_path):
    if not shutil.which("sqlite3"):
        pytest.skip("sqlite3 CLI not available")
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "profiles.db") as conn:
        conn.execute("create table t (x integer)")
        conn.execute("insert into t values (1)")
    backups = tmp_path / "backups"
    env = {**os.environ, "DATA_DIR": str(data), "BACKUP_DIR": str(backups), "BACKUP_KEEP": "1"}
    subprocess.run(["bash", "scripts/backup_data.sh"], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    first = list(backups.glob("yangya-data-*.tar.gz"))
    assert len(first) == 1
    time.sleep(1.1)
    subprocess.run(["bash", "scripts/backup_data.sh"], cwd=ROOT, env=env,
                   check=True, capture_output=True, text=True)
    assert len(list(backups.glob("yangya-data-*.tar.gz"))) == 1
