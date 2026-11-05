"""Synthetic browser-session regressions; no network listener or live services."""

import asyncio
from pathlib import Path
import subprocess

import httpx

from app.main import app


def test_decision_cookie_session_survives_refresh_and_reload(monkeypatch):
    monkeypatch.setattr("app.gateway.llm_gateway.chat_text", lambda *a, **k: "合成回复")
    message = "记住我下周三要出差去杭州。"

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://synthetic.invalid") as browser:
            session = await browser.post("/api/session")
            assert session.status_code == 200
            owner = session.json()["data"]["user_id"]
            turn = await browser.post("/api/chat", json={"message": message})
            assert turn.status_code == 200
            thread = turn.json()["thread_id"]
            decision = await browser.get(f"/api/confirm/{thread}")
            assert decision.status_code == 200, decision.text
            assert "decision" in decision.json()["data"]
            for _ in range(2):
                # A full reload repeats session initialization with the same cookie.
                reloaded = await browser.post("/api/session")
                assert reloaded.status_code == 200
                assert reloaded.json()["data"]["user_id"] == owner
                decision = await browser.get(f"/api/confirm/{thread}")
                assert decision.status_code == 200, decision.text
                profile = await browser.get(f"/api/profile/{owner}")
                assert profile.status_code == 200, profile.text
            assert (await browser.get("/api/confirm/synthetic-missing")).status_code == 404
            assert (await browser.get(f"/api/confirm/{thread}?user_id=synthetic-foreign")).status_code == 404
            browser.cookies.clear()
            assert (await browser.get(f"/api/confirm/{thread}")).status_code == 401
            replacement = await browser.post("/api/session")
            assert replacement.status_code == 200
            assert replacement.json()["data"]["user_id"] != owner
            assert (await browser.get(f"/api/confirm/{thread}")).status_code == 404

    asyncio.run(exercise())


def test_decision_errors_restart_and_reload_do_not_restick_wall():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "tests/session_wall_stick_ui.mjs"],
        cwd=root, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
