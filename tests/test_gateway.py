"""Unified LLM gateway: routing, fallback chain, retries, limits, usage, protocols."""

from __future__ import annotations

import inspect
import json
import threading
import time

import httpx
import pytest

from app.gateway import config as gw_config
from app.gateway import core, limiter, providers, usage
from app.gateway import llm_gateway as gw
from app.llm import ModelTier

KIMI = "https://api.moonshot.cn/v1"

RENDER_ENV = {
    "KIMI_API_KEY": "sk-kimi-test",
    "LLM_FAST_BASE_URL": KIMI,
    "LLM_FAST_MODEL": "kimi-k2.6",
    "LLM_STRONG_BASE_URL": KIMI,
    "LLM_STRONG_MODEL": "kimi-k3",
    "LLM_STRONG_FALLBACK_MODEL": "kimi-k2.6",
    "MODEL_FAST": "kimi-k2.6",
    "MODEL_STRONG": "kimi-k3",
}


class Upstream:
    """Scriptable fake vendor behind httpx.MockTransport."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.script: dict[str, list] = {}
        self.default = None
        self.delay = 0.0
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def on(self, model: str, *steps) -> "Upstream":
        self.script.setdefault(model, []).extend(steps)
        return self

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)

    def models_called(self) -> list[str]:
        return [json.loads(r.content)["model"] for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            model = json.loads(request.content)["model"]
            steps = self.script.get(model) or []
            step = steps.pop(0) if steps else (self.default or ok_text("ok"))
            if isinstance(step, Exception):
                raise step
            return step(request) if callable(step) else step
        finally:
            with self.lock:
                self.in_flight -= 1


def ok_text(content: str, pt: int = 10, ct: int = 5) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct},
        },
    )


def status(code: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(code, json={"error": {"message": f"status {code}"}}, headers=headers or {})


@pytest.fixture
def upstream(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith(gw_config._ENV_PREFIXES):
            monkeypatch.delenv(k, raising=False)
    for k, v in RENDER_ENV.items():
        monkeypatch.setenv(k, v)
    up = Upstream()
    providers.set_transport_factory(lambda: httpx.MockTransport(up.handler))
    gw_config.reset_config_cache()
    limiter.reset()
    usage.reset()
    sleeps: list[float] = []
    monkeypatch.setattr(core, "_sleep", lambda s: sleeps.append(s))
    up.sleeps = sleeps
    yield up
    providers.set_transport_factory(None)
    gw_config.reset_config_cache()
    limiter.reset()
    usage.reset()


# ---------- compatibility ----------


def test_chat_text_signature_compatible():
    params = inspect.signature(gw.chat_text).parameters
    assert list(params)[:2] == ["system", "user"]
    for name, default in [("tier", ModelTier.FAST), ("fallback", ""), ("timeout", None), ("task", None)]:
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default == default


def test_route_legacy_mapping(upstream):
    assert gw.route("strong") == ModelTier.STRONG
    assert gw.route("reason") == ModelTier.STRONG
    assert gw.route("chat") == ModelTier.FAST
    assert gw.route("draft") == ModelTier.FAST
    assert gw.route("summary") == ModelTier.FAST
    assert gw.route("unknown-task") == ModelTier.FAST


def test_default_config_is_two_tier_kimi(upstream):
    cfg = gw_config.get_config()
    assert cfg.tiers == {"fast": ["kimi-k2.6"], "strong": ["kimi-k3", "kimi-k2.6"]}
    for m in cfg.models.values():
        assert m.base_url == KIMI and m.has_key()


def test_app_llm_shim_routes_through_gateway(upstream):
    from app import llm

    upstream.on("kimi-k2.6", ok_text("via shim"))
    assert llm.chat_text("s", "u") == "via shim"


# ---------- request shape ----------


def test_fast_request_shape_kimi_k26(upstream):
    upstream.on("kimi-k2.6", ok_text("你好"))
    assert gw.chat_text("sys", "hi") == "你好"
    req = upstream.requests[0]
    assert str(req.url) == KIMI + "/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-kimi-test"
    body = upstream.body()
    assert body["model"] == "kimi-k2.6"
    assert body["thinking"] == {"type": "disabled"}
    assert "temperature" not in body
    assert body["messages"][0] == {"role": "system", "content": "sys\n回答简洁，不要废话。"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_strong_request_uses_k3_reasoning_effort(upstream, monkeypatch):
    upstream.on("kimi-k3", ok_text("draft"))
    assert gw.chat_text("s", "u", task="reason") == "draft"
    body = upstream.body()
    assert body["model"] == "kimi-k3"
    assert body["reasoning_effort"] == "high"
    monkeypatch.setenv("KIMI_K3_EFFORT", "low")
    upstream.on("kimi-k3", ok_text("draft2"))
    gw.chat_text("s", "u", tier=ModelTier.STRONG)
    assert upstream.body()["reasoning_effort"] == "low"


def test_non_kimi_sends_temperature_and_max_tokens(upstream, monkeypatch):
    monkeypatch.setenv(
        "LLM_CONFIG_JSON",
        json.dumps(
            {
                "models": {"ds": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY"}},
                "tiers": {"fast": ["ds"]},
            }
        ),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    upstream.on("deepseek-chat", ok_text("x"))
    gw.chat_text("s", "u", max_tokens=16)
    body = upstream.body()
    assert body["temperature"] == 0.4 and body["max_tokens"] == 16
    assert upstream.requests[0].headers["authorization"] == "Bearer sk-ds"


# ---------- fallback chain / retries ----------


def test_strong_5xx_retries_then_falls_back_to_next_model(upstream):
    upstream.on("kimi-k3", status(502), status(503), status(500))
    upstream.on("kimi-k2.6", ok_text("from k2.6"))
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "from k2.6"
    assert upstream.models_called() == ["kimi-k3"] * 3 + ["kimi-k2.6"]
    assert len(upstream.sleeps) == 2


def test_timeout_moves_to_next_model_without_retry(upstream):
    upstream.on("kimi-k3", httpx.ReadTimeout("slow"))
    upstream.on("kimi-k2.6", ok_text("fallback"))
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "fallback"
    assert upstream.models_called() == ["kimi-k3", "kimi-k2.6"]
    errs = [r["error"] for r in usage.recent()]
    assert errs == ["timeout", ""]


def test_retry_on_timeout_opt_in(upstream, monkeypatch):
    monkeypatch.setenv("LLM_RETRY_ON_TIMEOUT", "1")
    upstream.on("kimi-k2.6", httpx.ReadTimeout("slow"), ok_text("second"))
    assert gw.chat_text("s", "u") == "second"


def test_429_honours_retry_after_capped(upstream, monkeypatch):
    monkeypatch.setenv("LLM_RETRY_AFTER_MAX", "3")
    upstream.on("kimi-k2.6", status(429, {"retry-after": "30"}), ok_text("ok after 429"))
    assert gw.chat_text("s", "u") == "ok after 429"
    assert 3.0 <= upstream.sleeps[0] <= 3.25


def test_unexpected_exception_still_falls_back_to_next_model(upstream, monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    upstream.on("kimi-k3", RuntimeError("boom"))
    upstream.on("kimi-k2.6", ok_text("from k2.6"))
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "from k2.6"
    assert upstream.models_called() == ["kimi-k3", "kimi-k2.6"]


def test_upstream_failure_without_fallback_is_not_mock_text(upstream):
    upstream.default = status(500)
    out = gw.chat_text("s", "u")
    assert out == gw.UPSTREAM_ERROR_TEXT and "[mock-llm]" not in out


def test_auth_error_not_retried(upstream):
    upstream.on("kimi-k2.6", status(401))
    assert gw.chat_text("s", "u", fallback="FB") == "FB"
    assert len(upstream.requests) == 1


def test_all_fail_returns_fallback(upstream):
    upstream.default = status(500)
    assert gw.chat_text("s", "u", fallback="稍后再试") == "稍后再试"
    assert len(upstream.requests) == 3


def test_empty_fast_escalates_to_strong(upstream):
    upstream.on("kimi-k2.6", ok_text(""))
    upstream.on("kimi-k3", ok_text("strong answer"))
    assert gw.chat_text("s", "u") == "strong answer"
    assert upstream.models_called() == ["kimi-k2.6", "kimi-k3"]


def test_empty_escalate_can_be_disabled(upstream, monkeypatch):
    monkeypatch.setenv("LLM_EMPTY_ESCALATE", "0")
    upstream.on("kimi-k2.6", ok_text(""))
    assert gw.chat_text("s", "u", fallback="FB") == "FB"
    assert upstream.models_called() == ["kimi-k2.6"]


def test_strong_empty_k3_falls_back_to_k26(upstream):
    upstream.on("kimi-k3", ok_text(""))
    upstream.on("kimi-k2.6", ok_text("k2.6 answer"))
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "k2.6 answer"


def test_total_deadline_bounds_latency(upstream, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(core, "_now", lambda: clock[0])

    def slow(_req):
        clock[0] += 8.0
        raise httpx.ReadTimeout("slow")

    upstream.default = slow
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({"tiers": {"strong": ["kimi-k3", "kimi-k2.6", "kimi-k3"]}}))
    gw.chat_text("s", "u", tier=ModelTier.STRONG, timeout=8, fallback="FB")
    assert len(upstream.requests) == 2  # 8s timeout × factor 2 = 16s budget


# ---------- no key / mock ----------


def test_no_key_returns_fallback_and_makes_no_request(upstream, monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY")
    assert gw.chat_text("s", "u", fallback="FB") == "FB"
    assert gw.chat_text("s", "hello").startswith("[mock-llm]")
    out = gw.chat_json("s", "u", fallback={"a": 1})
    assert out["a"] == 1 and out["_mode"] == "mock"
    assert upstream.requests == []
    assert gw.has_llm() is False


# ---------- chat_json ----------


def test_chat_json_parses_fenced_json(upstream):
    upstream.on("kimi-k2.6", ok_text('```json\n{"score": 80}\n```'))
    out = gw.chat_json("s", "u", fallback={"score": 0})
    assert out["score"] == 80 and out["_model"] == "kimi-k2.6" and out["_tier"] == "fast"
    assert upstream.body()["messages"][0]["content"].endswith("只输出合法 JSON，不要 markdown 围栏。回答尽量短。")


def test_chat_json_error_uses_fallback(upstream):
    upstream.default = status(500)
    out = gw.chat_json("s", "u", fallback={"score": 0})
    assert out["score"] == 0 and out["_mode"] == "mock_fallback" and "_model_error" in out


def test_chat_json_bad_json_uses_fallback(upstream):
    upstream.on("kimi-k2.6", ok_text("not json"))
    out = gw.chat_json("s", "u", fallback={"score": 0})
    assert out["_parse"] == "fallback"


def test_chat_json_reports_model_actually_used(upstream):
    upstream.on("kimi-k3", status(500), status(500), status(500))
    upstream.on("kimi-k2.6", ok_text('{"ok": true}'))
    out = gw.chat_json("s", "u", tier=ModelTier.STRONG)
    assert out["ok"] is True and out["_model"] == "kimi-k2.6"


# ---------- config sources ----------


def test_config_file_json_multi_provider(upstream, monkeypatch, tmp_path):
    doc = {
        "providers": {
            "moonshot": {"protocol": "openai", "base_url": KIMI, "api_key_env": "KIMI_API_KEY", "max_concurrency": 4},
            "claude": {"protocol": "anthropic", "base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY"},
        },
        "models": {
            "k26": {"provider": "moonshot", "model": "kimi-k2.6", "price": {"input": 4, "output": 16, "currency": "CNY"}},
            "sonnet": {"provider": "claude", "model": "claude-sonnet-4-5", "max_tokens": 2048,
                       "price": {"input": 3, "output": 15, "currency": "USD"}},
        },
        "tiers": {"fast": ["k26"], "strong": ["sonnet", "k26"]},
        "tasks": {"greeting": "strong"},
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setenv("LLM_CONFIG_FILE", str(path))
    cfg = gw_config.get_config()
    assert cfg.tiers["strong"] == ["sonnet", "k26"]
    assert cfg.models["sonnet"].protocol == "anthropic"
    assert gw.route("greeting") == ModelTier.STRONG
    assert gw_config.get_config().groups["moonshot"]["max_concurrency"] == 4
    # sonnet has no key → skipped, k26 serves strong
    upstream.on("kimi-k2.6", ok_text("k26"))
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "k26"


def test_config_file_toml(upstream, monkeypatch, tmp_path):
    path = tmp_path / "models.toml"
    path.write_text(
        '[models.qwen]\nbase_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"\n'
        'model = "qwen-plus"\napi_key_env = "DASHSCOPE_API_KEY"\n'
        '[tiers]\nfast = ["qwen", "kimi-k2.6"]\n'
    )
    monkeypatch.setenv("LLM_CONFIG_FILE", str(path))
    assert gw_config.get_config().tiers["fast"] == ["qwen", "kimi-k2.6"]
    upstream.on("kimi-k2.6", ok_text("kimi"))
    assert gw.chat_text("s", "u") == "kimi"  # qwen has no key → skipped


def test_chain_env_override(upstream, monkeypatch):
    monkeypatch.setenv("LLM_FAST_CHAIN", "kimi-k3,kimi-k2.6")
    assert gw_config.get_config().tiers["fast"] == ["kimi-k3", "kimi-k2.6"]


def test_config_validation_rejects_unknown_model(upstream, monkeypatch):
    monkeypatch.setenv("LLM_FAST_CHAIN", "nope")
    with pytest.raises(gw_config.ConfigError):
        gw_config.get_config()


def test_config_validation_rejects_bad_protocol(upstream, monkeypatch):
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({"models": {"x": {"base_url": "http://x", "protocol": "grpc"}}}))
    with pytest.raises(gw_config.ConfigError):
        gw_config.get_config()


def test_public_config_has_no_secrets(upstream):
    blob = json.dumps(gw.gateway_info(), ensure_ascii=False)
    assert "sk-kimi-test" not in blob


# ---------- anthropic protocol ----------


def test_anthropic_protocol_request_and_usage(upstream, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv(
        "LLM_CONFIG_JSON",
        json.dumps(
            {
                "models": {
                    "claude": {
                        "protocol": "anthropic",
                        "model": "claude-sonnet-4-5",
                        "api_key_env": "ANTHROPIC_API_KEY",
                        "price": {"input": 3, "output": 15, "currency": "USD"},
                    }
                },
                "tiers": {"strong": ["claude", "kimi-k3"]},
            }
        ),
    )
    upstream.on(
        "claude-sonnet-4-5",
        httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hello from claude"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            },
        ),
    )
    assert gw.chat_text("sys", "u", tier=ModelTier.STRONG) == "hello from claude"
    req = upstream.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant"
    assert req.headers["anthropic-version"] == providers.ANTHROPIC_VERSION
    body = upstream.body()
    assert body["system"].startswith("sys") and body["messages"] == [{"role": "user", "content": "u"}]
    assert body["max_tokens"] == 1024 and body["temperature"] == 0.4
    rec = usage.recent()[-1]
    assert rec["prompt_tokens"] == 1000 and rec["completion_tokens"] == 200
    assert rec["cost"] == pytest.approx((1000 * 3 + 200 * 15) / 1e6) and rec["currency"] == "USD"


def test_anthropic_overloaded_falls_back_to_openai_compat(upstream, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv(
        "LLM_CONFIG_JSON",
        json.dumps({"models": {"claude": {"protocol": "anthropic", "model": "claude-x", "api_key_env": "ANTHROPIC_API_KEY"}},
                    "tiers": {"strong": ["claude", "kimi-k3"]}}),
    )
    upstream.on("claude-x", status(529), status(529), status(529))
    upstream.on("kimi-k3", ok_text("kimi took over"))
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "kimi took over"


# ---------- usage ----------


def test_usage_records_model_latency_tokens_cost(upstream, monkeypatch):
    monkeypatch.setenv("LLM_FAST_PRICE_INPUT", "4")
    monkeypatch.setenv("LLM_FAST_PRICE_OUTPUT", "16")
    upstream.on("kimi-k2.6", ok_text("hi", pt=1000, ct=500))
    gw.chat_text("s", "u", task="chat")
    rec = usage.recent()[-1]
    assert rec["model"] == "kimi-k2.6" and rec["task"] == "chat" and rec["tier"] == "fast"
    assert rec["ok"] and rec["latency_ms"] >= 0
    assert rec["prompt_tokens"] == 1000 and rec["completion_tokens"] == 500
    assert rec["cost"] == pytest.approx((1000 * 4 + 500 * 16) / 1e6) and rec["currency"] == "CNY"
    tot = usage.totals()["kimi-k2.6"]
    assert tot["calls"] == 1 and tot["ok"] == 1 and tot["prompt_tokens"] == 1000


def test_usage_cost_none_without_price(upstream):
    upstream.on("kimi-k2.6", ok_text("hi"))
    gw.chat_text("s", "u")
    assert usage.recent()[-1]["cost"] is None


def test_usage_jsonl_file(upstream, monkeypatch, tmp_path):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("LLM_USAGE_LOG", str(path))
    upstream.on("kimi-k2.6", status(500), ok_text("ok"))
    gw.chat_text("s", "u")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["ok"] for x in lines] == [False, True]
    assert lines[0]["status"] == 500 and lines[0]["request_id"] == lines[1]["request_id"]


# ---------- concurrency ----------


def test_http_client_is_reused(upstream):
    for _ in range(5):
        gw.chat_text("s", "u")
    gw.chat_text("s", "u", tier=ModelTier.STRONG)
    assert providers.pool_stats()["clients"] == 1


def test_concurrency_cap_per_account(upstream, monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    upstream.delay = 0.05
    results: list[str] = []

    def worker(i):
        tier = ModelTier.STRONG if i % 2 else ModelTier.FAST
        results.append(gw.chat_text("s", f"u{i}", tier=tier))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 8
    assert upstream.peak <= 2  # k2.6 + k3 share one Moonshot bucket


def test_busy_bucket_falls_through_to_other_account(upstream, monkeypatch):
    monkeypatch.setenv("LLM_QUEUE_TIMEOUT", "0.01")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    monkeypatch.setenv(
        "LLM_CONFIG_JSON",
        json.dumps(
            {
                "providers": {"moonshot": {"base_url": KIMI, "api_key_env": "KIMI_API_KEY", "max_concurrency": 1}},
                "models": {
                    "k26": {"provider": "moonshot", "model": "kimi-k2.6"},
                    "ds": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY"},
                },
                "tiers": {"fast": ["k26", "ds"]},
            }
        ),
    )
    spec = gw_config.get_config().models["k26"]
    b = limiter.bucket(spec.group(), 1, 0)
    with b.slot(0):
        upstream.on("deepseek-chat", ok_text("ds served"))
        assert gw.chat_text("s", "u") == "ds served"
    assert [r["error"] for r in usage.recent()] == ["busy", ""]


def test_rpm_bucket_rejects_when_exhausted():
    b = limiter._Bucket(max_concurrency=4, rpm=1)
    with b.slot(0.0):
        pass
    with pytest.raises(limiter.Busy):
        with b.slot(0.0):
            pass


def test_adaptive_window_shrinks_on_429_and_recovers():
    b = limiter._Bucket(max_concurrency=8, rpm=0)
    for _ in range(3):
        b.feedback(ok=False, rate_limited=True)
    assert b._limit() == 2
    for _ in range(200):
        b.feedback(ok=True, rate_limited=False)
    assert b._limit() == 8


def test_429_storm_queues_instead_of_failing(upstream, monkeypatch):
    """Upstream allows 2 concurrent; gateway cap 8 adapts down and every call still succeeds."""
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "8")
    real_sleep = time.sleep
    monkeypatch.setattr(core, "_sleep", lambda s: real_sleep(min(s, 0.02)))
    upstream.delay = 0.03
    gate = threading.BoundedSemaphore(2)

    def limited(_req):
        if not gate.acquire(blocking=False):
            return status(429, {"retry-after": "0"})
        try:
            real_sleep(0.03)
            return ok_text("ok")
        finally:
            gate.release()

    upstream.default = limited
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(gw.chat_text("s", "u", fallback="FB"))) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 16
    st = limiter.stats()
    assert next(iter(st.values()))["throttled_429"] >= 1


def test_usage_endpoint(upstream):
    from fastapi.testclient import TestClient

    from app.main import app

    upstream.on("kimi-k2.6", ok_text("hi"))
    gw.chat_text("s", "u", task="chat")
    r = TestClient(app).get("/api/llm/usage", params={"limit": 5})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["totals"]["kimi-k2.6"]["calls"] == 1
    assert data["recent"][-1]["task"] == "chat"
    assert "sk-kimi-test" not in r.text


def test_example_config_file_loads(upstream, monkeypatch):
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs" / "llm_models.example.json"
    monkeypatch.setenv("LLM_CONFIG_FILE", str(path))
    cfg = gw_config.get_config()
    assert cfg.tiers["fast"] == ["kimi-k2.6", "deepseek-chat"]
    assert cfg.models["claude-sonnet"].protocol == "anthropic"
    assert cfg.models["kimi-k3"].group() == cfg.models["kimi-k2.6"].group() == "moonshot"
    upstream.on("kimi-k2.6", ok_text("hi", pt=1_000_000, ct=0))
    gw.chat_text("s", "u")
    assert usage.recent()[-1]["cost"] == pytest.approx(4.0)


# ---------- per-tier extra params (legacy env) ----------


def test_tier_params_env_merged_into_request_body(upstream, monkeypatch):
    monkeypatch.setenv("LLM_FAST_PARAMS", '{"enable_thinking": false}')
    gw_config.reset_config_cache()
    assert gw.chat_text("s", "u", tier=ModelTier.FAST) == "ok"
    assert upstream.body()["enable_thinking"] is False
    # STRONG has no params env: body unchanged apart from the Kimi defaults.
    gw.chat_text("s", "u", tier=ModelTier.STRONG)
    assert "enable_thinking" not in upstream.body()


def test_tier_params_env_invalid_is_ignored(upstream, monkeypatch, caplog):
    monkeypatch.setenv("LLM_FAST_PARAMS", "not json")
    monkeypatch.setenv("LLM_STRONG_PARAMS", "[1, 2]")
    gw_config.reset_config_cache()
    assert gw.chat_text("s", "u", tier=ModelTier.FAST) == "ok"
    assert "enable_thinking" not in upstream.body()
    assert gw.chat_text("s", "u", tier=ModelTier.STRONG) == "ok"
    assert any("PARAMS ignored" in r.message for r in caplog.records)
