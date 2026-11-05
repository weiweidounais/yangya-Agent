"""Offline test harness: isolated DATA_DIR, no LLM keys, no background threads."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from typing import Any, Optional

# Channel tests import shared helpers as `conftest`, including in package mode.
sys.modules.setdefault("conftest", sys.modules[__name__])

# Isolated data dir + vault key must be in place before app modules import (DATA_DIR is read at import).
_TMP = Path(tempfile.mkdtemp(prefix="personal-agent-tests-"))
os.environ["DATA_DIR"] = str(_TMP)
# A filled .env must never leak real model keys into the suite: app.llm calls
# load_dotenv() at import, which would re-add the keys scrubbed below.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.pop("SECRETS_ALLOW_DEMO", None)
for _k in (
    "LLM_FAST_API_KEY",
    "LLM_STRONG_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
):
    os.environ.pop(_k, None)
os.environ["PROACTIVE_ENABLED"] = "0"
for _k in list(os.environ):
    if _k.endswith("_API_KEY") or _k in ("LLM_FAST_MODEL", "LLM_STRONG_MODEL"):
        os.environ.pop(_k, None)

from cryptography.fernet import Fernet  # noqa: E402

TEST_KEY = Fernet.generate_key().decode()
os.environ["SECRETS_FERNET_KEY"] = TEST_KEY

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SECRET_KEYS = (
    "SILICONFLOW_API_KEY",
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "LLM_FAST_API_KEY",
    "LLM_STRONG_API_KEY",
)
_CHANNEL_KEYS = (
    "FEISHU_ENABLED",
    "FEISHU_WS_ENABLED",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "OPENCLAW_WECHAT_ENABLED",
    "OPENCLAW_WEBHOOK_TOKEN",
    "OPENCLAW_GATEWAY_URL",
    "OPENCLAW_GATEWAY_TOKEN",
    "OPENCLAW_SYNC_WAIT_SECONDS",
    "CHANNEL_ADMIN_TOKEN",
    "FEISHU_BIND_USER_ID",
    "FEISHU_BIND_OPEN_IDS",
    "CHANNEL_AUTO_BIND",
    "PROACTIVE_ENABLED",
)


def _scrub() -> None:
    for key in _SECRET_KEYS + _CHANNEL_KEYS:
        os.environ.pop(key, None)


_scrub()

os.environ["PROACTIVE_ENABLED"] = "0"
os.environ["PUBLIC_BASE_URL"] = "https://agent.example.test"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _TMP

os.environ.setdefault("PROACTIVE_TZ", "Asia/Shanghai")
for key in ("OPENAI_BASE_URL", "LLM_BASE_URL"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["FEISHU_WS_ENABLED"] = "false"


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def vault_key(monkeypatch):
    monkeypatch.setenv("SECRETS_FERNET_KEY", TEST_KEY)
    return TEST_KEY


class FakeLLM:
    """Records every prompt and returns canned replies for chat turns."""

    def __init__(self, reply: str = "好的"):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def chat_text(self, system, user, **kwargs):
        self.calls.append((system, user))
        if str(system).startswith("你是心理安全分类器"):
            return "NO"
        return self.reply

    def all_text(self) -> str:
        return "\n".join(s + "\n" + u for s, u in self.calls)


@pytest.fixture
def fake_llm(monkeypatch):
    from app.gateway import llm_gateway

    fake = FakeLLM()
    monkeypatch.setattr(llm_gateway, "chat_text", fake.chat_text)
    return fake

import uuid  # noqa: E402

_BUDGET_ENV = (
    "AUTH_SECRET",
    "AGENT_MAX_STEPS",
    "AGENT_MAX_RETRIES",
    "AGENT_MAX_REDRAFTS",
    "AGENT_MAX_SECONDS",
    "AGENT_MAX_TOKENS",
    "AGENT_COST_PER_1K_TOKENS",
    "AGENT_MAX_COST",
    "AGENT_COST_SOFT_LIMIT",
    "AGENT_TIER_ESCALATION",
    "AGENT_PAID_USERS",
    "AGENT_COST_TZ",
    *(
        f"AGENT_BUDGET_{t}_{d}"
        for t in ("SIMPLE", "MEDIUM", "HARD")
        for d in ("STEPS", "SECONDS", "TOKENS", "COST")
    ),
    *(f"AGENT_COST_CAP_{p}_{s}" for p in ("FREE", "PAID") for s in ("DAILY", "MONTHLY")),
)


def _init_tables() -> None:
    from app import db
    from app.agent.harness.traces import TraceStep  # noqa: F401
    from app.agent.memory.episodic import EpisodicEvent  # noqa: F401
    from app.confirm_store import ConfirmDecision  # noqa: F401

    db.init_db()
    db.Base.metadata.create_all(db.ENGINE)


_init_tables()


@pytest.fixture(autouse=True)
def _clean_budget_env(monkeypatch):
    for k in _BUDGET_ENV:
        monkeypatch.delenv(k, raising=False)
    # S1/#5: the API is closed without AUTH_SECRET; tests run with one by default.
    monkeypatch.setenv("AUTH_SECRET", "test-auth-secret-0123456789abcdef")
    yield


@pytest.fixture
def uid():
    """Identity for direct test calls; HTTP requests still authenticate independently."""
    from app.data_access import user_scope
    value = f"u-{uuid.uuid4().hex[:10]}"
    with user_scope(value):
        yield value


@pytest.fixture(autouse=True)
def _drain_background_summaries(monkeypatch):
    # Chat/draft calls enqueue summaries after returning. Keep their
    # profile_read audits inside the originating test, before patches are undone.
    from app.agent.harness import traces
    from app.agent.memory.summary import get_scheduler, reset_summary_scheduler

    def drain():
        scheduler = get_scheduler()
        thread = scheduler._thread
        try:
            assert scheduler.flush(5.0), "background summaries did not finish"
        finally:
            scheduler.shutdown()
        assert thread is None or not thread.is_alive(), "summary worker did not stop"
        # Completed summaries/tools can still have audits waiting in the writer's
        # batch. Redaction reads the recorded owner's profile asynchronously.
        assert traces.flush(5.0), "background trace records did not finish"
        assert traces.writer_stats()["pending"] == 0, "trace records remain after drain"
        # Discard test-owned generators before mocks or database bindings are restored.
        reset_summary_scheduler()

    yield drain
    drain()


@pytest.fixture
def auth(monkeypatch):
    """Enable AUTH_SECRET and return a header factory for a user."""
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    from app.auth import mint_user_token

    def _headers(user_id: str, ttl: int | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {mint_user_token(user_id, ttl)}"}

    return _headers


from app.channels.base import ChannelAdapter, ChannelReply  # noqa: E402
from app.data_access.channel_identity import resolve_channel_identity as resolve_user_id  # noqa: E402
from app.channels.registry import _ADAPTERS, register_adapter  # noqa: E402

DRAFT_REQUEST = "帮我写一封请假邮件，周五请一天假"


class MockAdapter(ChannelAdapter):
    """In-memory channel: records every push. ``cards`` toggles Feishu-like vs WeChat-like."""

    display_name = "测试渠道"

    def __init__(self, name: str, *, cards: bool) -> None:
        self.name = name
        self.supports_cards = cards
        self.sent: list[dict[str, Any]] = []

    def enabled(self) -> bool:
        return True

    def config_status(self) -> dict[str, Any]:
        return {"mock": True}

    def render(self, reply: ChannelReply) -> dict[str, Any]:
        if self.supports_cards:
            from app.channels.feishu import render_feishu_card

            return {"text": reply.text, "card": render_feishu_card(reply)}
        from app.channels.openclaw_wechat import render_wechat_text

        return {"text": render_wechat_text(reply), "card": None}

    def send(
        self,
        *,
        external_user_id: str,
        rendered: dict[str, Any],
        message_id: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self.sent.append({"to": external_user_id, **rendered})
        return {"ok": True, "via": "mock"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _SECRET_KEYS + _CHANNEL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agent.example.test")
    yield


@pytest.fixture
def mock_text_channel():
    adapter = register_adapter(MockAdapter("mock_text", cards=False))
    yield adapter
    _ADAPTERS.pop("mock_text", None)


@pytest.fixture
def mock_card_channel():
    adapter = register_adapter(MockAdapter("mock_card", cards=True))
    yield adapter
    _ADAPTERS.pop("mock_card", None)


def make_inbound(
    channel: str,
    external: str,
    text: str = "",
    *,
    action_value: Optional[dict[str, Any]] = None,
    is_group: bool = False,
    message_id: str = "m1",
) -> dict[str, Any]:
    uid = resolve_user_id(channel, external)
    inbound: dict[str, Any] = {
        "channel": channel,
        "external_user_id": external,
        "text": text,
        "message_id": message_id,
        "chat_id": "",
        "user_id": uid,
        "bound": bool(uid),
        "is_group": is_group,
        "thread_id": "",
        "raw": {"text": text},
    }
    if action_value:
        inbound["action_value"] = action_value
    return {"type": "inbound", "inbound": inbound, "kind": "message"}


@pytest.fixture
def make_channel_inbound():
    """Resolve the shared factory through pytest, independent of conftest imports."""
    return make_inbound


def send(channel: str, external: str, text: str = "", **kw: Any) -> dict[str, Any]:
    from app.channels.gateway import handle_inbound

    return handle_inbound(make_inbound(channel, external, text, **kw), channel)


def token_from(url: str) -> str:
    return url.split("t=", 1)[1]


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def profile(uid):
    from app import db

    db.init_db()
    return db.save_profile(uid, {
        "name": "Li Lei",
        "email": "lilei@example.com",
        "phone": "+86 138 0000 0000",
        "city": "上海",
        "occupation": "数据分析师",
        "interests": ["跑步", "摄影"],
        "preferences": ["早上不要打扰"],
        "facts": ["下周三去杭州出差"],
    })


# ----- S1/#5 session identity: test plumbing -----
# Production derives the user only from a server-signed session. Legacy API
# tests were written as "the caller is whoever user_id says"; SessionTestClient
# keeps them meaningful by signing a session for exactly that claimed user.
# Cross-account tests must use RawTestClient and explicit tokens.
import re as _re  # noqa: E402
from urllib.parse import parse_qs as _parse_qs, urlsplit as _urlsplit  # noqa: E402

os.environ.setdefault("AUTH_SECRET", "test-auth-secret-0123456789abcdef")

import starlette.testclient as _st  # noqa: E402
import fastapi.testclient as _ft  # noqa: E402

RawTestClient = _st.TestClient
_PATH_USER = _re.compile(r"^/api/(profile|memory)/([^/?]+)")


def _claimed_user(method: str, url: str, kw: dict) -> str:
    for src in (kw.get("json"), kw.get("params"), kw.get("data")):
        if isinstance(src, dict) and "user_id" in src and src["user_id"] is not None:
            return str(src["user_id"]).strip()
    parts = _urlsplit(str(url))
    q = _parse_qs(parts.query, keep_blank_values=True)
    if "user_id" in q:
        return q["user_id"][0].strip()
    m = _PATH_USER.match(parts.path)
    if m:
        return m.group(2)
    return "demo"


class SessionTestClient(RawTestClient):
    def request(self, method, url, **kw):
        from app.auth import auth_configured, mint_user_token

        headers = dict(kw.pop("headers", None) or {})
        has_auth = any(k.lower() in ("authorization", "x-user-token") for k in headers)
        if not has_auth and auth_configured() and str(url).split("?")[0].startswith("/api/"):
            uid = _claimed_user(method, url, kw)
            if uid:  # an explicitly blank user_id gets no session (→ 401)
                headers["X-User-Token"] = mint_user_token(uid)
            js = kw.get("json")
            if (
                method.upper() == "POST"
                and str(url).split("?")[0] == "/api/confirm"
                and isinstance(js, dict)
                and "confirm_token" not in js
            ):
                r = super().request(
                    "POST", f"/api/confirm/{js.get('thread_id')}/token", headers=headers
                )
                if r.status_code == 200:
                    kw["json"] = {**js, "confirm_token": r.json()["data"]["confirm_token"]}
        return super().request(method, url, headers=headers, **kw)


_st.TestClient = SessionTestClient
_ft.TestClient = SessionTestClient


@pytest.fixture(autouse=True)
def _intent_rule_baseline(monkeypatch):
    """Legacy regression baseline; scoring tests explicitly enable mocked hybrid."""
    monkeypatch.setenv("INTENT_SCORING_MODE", "rules")
