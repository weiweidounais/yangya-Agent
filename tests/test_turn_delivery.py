"""Offline delivery recovery: exact terminal responses, never intermediate graph state."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx

from app import db, main, turn_results
from app.data_access.session import memory_session
from app.models import ApiResponse


class LocalClient:
    """In-process ASGI requests without TestClient's cross-thread portal.

    A short bounded clock tick also wakes worker completions in sandboxes where
    asyncio's cross-thread socket wakeup is unavailable. No network hosts or
    application/authorization code are replaced.
    """

    def __init__(self, app, raise_server_exceptions=True):
        self.transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_server_exceptions)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        from app.auth import mint_user_token

        if not kwargs.get("headers"):
            kwargs["headers"] = {"X-User-Token": mint_user_token(kwargs.get("json", {}).get("user_id", "demo"))}

        async def send():
            async with httpx.AsyncClient(transport=self.transport, base_url="http://synthetic.invalid") as client:
                task = asyncio.create_task(client.request(method, url, **kwargs))
                for _ in range(500):
                    if task.done():
                        return task.result()
                    await asyncio.sleep(0.01)
                task.cancel()
                raise AssertionError("local ASGI request did not complete in five seconds")

        return asyncio.run(send())

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


@pytest.mark.parametrize("endpoint,handler,content", [
    ("chat", "_chat", {"message": "帮我写一封请假邮件，周五请一天假"}),
    ("draft", "_draft", {"request": "帮我写一封请假邮件，周五请一天假"}),
])
def test_multistep_turn_can_be_polled_after_original_wait_ends(monkeypatch, uid, auth, endpoint, handler, content):
    started, redraft, analyzed, finish = (Event() for _ in range(4))
    calls = []
    result = ApiResponse(thread_id="synthetic-thread", status="awaiting_confirm", awaiting_confirm=True,
                         data={"draft": {"kind": "email", "title": "请假", "body": "测试草稿"},
                               "messages": [{"role": "assistant", "content": "草稿写好了"}]})

    def work(body):
        calls.append(body)
        started.set()
        assert redraft.wait(5)
        analyzed.set()  # Analysis finished; a self-check/strong-model redraft is still running.
        assert finish.wait(5)
        return result

    monkeypatch.setattr(main, handler, work)
    request_id = uuid4().hex
    with LocalClient(main.app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/{endpoint}",
                              json={"user_id": uid, "request_id": request_id, **content})
        try:
            assert started.wait(5)
            url = f"/api/turns/{request_id}"
            assert client.get(url, headers=auth(uid)).json() == {"status": "running"}
            redraft.set()
            assert analyzed.wait(5)
            assert client.get(url, headers=auth(uid)).json() == {"status": "running"}
            assert client.get(url, headers=auth("synthetic-other-user")).status_code == 404
        finally:
            redraft.set()
            finish.set()
        original = pending.result(timeout=5)
        assert original.status_code == 200
        # Recovery uses a fresh request/session, not the original response connection.
        with LocalClient(main.app) as recovery:
            delivered = recovery.get(url, headers=auth(uid))
            assert delivered.headers["cache-control"] == "no-store"
            body = delivered.json()
            assert body["status"] == "completed"
            assert body["response"].pop("traceId") == original.headers["x-trace-id"]
            assert body["response"] == result.model_dump()
        replay = client.post(f"/api/{endpoint}", json={"user_id": uid, "request_id": request_id, **content})
        assert replay.status_code == 409
        assert len(calls) == 1


def test_failed_turn_is_terminal_and_does_not_expose_exception(monkeypatch, uid, auth):
    def fail(body):
        raise RuntimeError("synthetic internal failure detail")

    monkeypatch.setattr(main, "_chat", fail)
    request_id = uuid4().hex
    with LocalClient(main.app, raise_server_exceptions=False) as client:
        response = client.post("/api/chat", json={"user_id": uid, "request_id": request_id, "message": "随便聊聊"})
        assert response.status_code == 500
        response = client.get(f"/api/turns/{request_id}", headers=auth(uid))
        assert response.json() == {"status": "failed", "code": "turn_failed"}
        assert "internal" not in response.text


def test_abandoned_worker_expires_but_a_saved_result_is_still_recoverable(monkeypatch, uid):
    request_id = uuid4().hex
    turn_results.init_db()
    with memory_session() as session:
        session.add(turn_results.TurnResult(user_id=uid, request_id=request_id, started_at=100))
    monkeypatch.setattr(turn_results, "time", SimpleNamespace(time=lambda: 100 + turn_results.TURN_TIMEOUT))
    assert turn_results.get(request_id) == {"status": "failed", "code": "turn_timeout"}
    turn_results._finish(uid, request_id, "completed", {"status": "done", "data": {"messages": []}})
    assert turn_results.get(request_id)["status"] == "completed"


def test_recovery_requires_session_and_request_id_is_validated(uid):
    with LocalClient(main.app) as client:
        response = client.get(f"/api/turns/{uuid4().hex}", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
        response = client.post("/api/chat", json={"user_id": uid, "request_id": "invalid", "message": "测试"})
        assert response.status_code == 422


def test_legacy_chat_contract_is_unchanged(monkeypatch, uid):
    result = ApiResponse(status="chat", data={"messages": [{"role": "assistant", "content": "测试回复"}]})
    monkeypatch.setattr(main, "_chat", lambda body: result)
    with LocalClient(main.app) as client:
        response = client.post("/api/chat", json={"user_id": uid, "message": "测试"})
        assert response.json() == result.model_dump()


def test_real_offline_draft_result_survives_lost_http_response(uid, auth):
    db.save_profile(uid, {"name": "测试用户甲", "city": "杭州", "occupation": "后端工程师"})
    request_id = uuid4().hex

    async def drop_response(scope, receive, send):
        async def disconnected(message):
            if message["type"] == "http.response.start":
                raise ConnectionError("synthetic disconnected browser")
            await send(message)

        await main.app(scope, receive, disconnected)

    with LocalClient(drop_response, raise_server_exceptions=False) as disconnected:
        lost = disconnected.post("/api/chat", json={
            "user_id": uid, "request_id": request_id,
            "message": "帮我写一封请假邮件，周五请一天假",
        })
        assert lost.status_code == 500
    with LocalClient(main.app) as client:
        recovered = client.get(f"/api/turns/{request_id}", headers=auth(uid)).json()
        assert recovered["status"] == "completed"
        result = recovered["response"]
        assert result["status"] == "awaiting_confirm"
        assert result["awaiting_confirm"] is True
        assert result["data"]["task_card"]["type"] == "draft_confirm"
        assert result["data"]["draft"]["body"] and "测试用户甲" in result["data"]["draft"]["body"]
        assert result["data"]["messages"]
        assert client.get(f"/api/turns/{request_id}", headers=auth("synthetic-other-user")).status_code == 404
