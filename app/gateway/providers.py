"""Wire protocols: OpenAI-compatible ``/chat/completions`` and Anthropic ``/v1/messages``.

One pooled ``httpx.Client`` per (base_url, api_key) so concurrent requests reuse
TLS connections instead of opening a fresh client per call.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.gateway.config import PROTOCOL_ANTHROPIC, ModelSpec

ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class Completion:
    content: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    finish_reason: str = ""
    logprobs: Optional[dict[str, Any]] = None


class ProviderError(Exception):
    """Classified upstream failure.

    kind: timeout | connect | rate_limited | server | auth | bad_request | empty | busy | no_key
    """

    RETRYABLE = frozenset({"connect", "rate_limited", "server"})

    def __init__(self, kind: str, message: str = "", *, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


_clients: dict[str, httpx.Client] = {}
_clients_lock = threading.Lock()


def _client(base_url: str, api_key: str) -> httpx.Client:
    key = base_url + "|" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    c = _clients.get(key)
    if c is not None and not c.is_closed:
        return c
    with _clients_lock:
        c = _clients.get(key)
        if c is None or c.is_closed:
            c = httpx.Client(
                limits=httpx.Limits(
                    max_connections=_int_env("LLM_HTTP_MAX_CONNECTIONS", 32),
                    max_keepalive_connections=_int_env("LLM_HTTP_MAX_KEEPALIVE", 16),
                    keepalive_expiry=30.0,
                ),
                transport=_transport_factory(),
            )
            _clients[key] = c
    return c


def _default_transport() -> Optional[httpx.BaseTransport]:
    return None


# Tests swap this for an httpx.MockTransport factory.
_transport_factory = _default_transport


def set_transport_factory(factory) -> None:
    global _transport_factory
    _transport_factory = factory or _default_transport
    close_all()


def close_all() -> None:
    with _clients_lock:
        for c in _clients.values():
            try:
                c.close()
            except Exception:
                pass
        _clients.clear()


def pool_stats() -> dict[str, Any]:
    return {"clients": len(_clients)}


def _retry_after(resp: httpx.Response) -> Optional[float]:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _raise_for_status(resp: httpx.Response) -> None:
    s = resp.status_code
    if s < 400:
        return
    body = resp.text[:200]
    if s == 429:
        raise ProviderError("rate_limited", body, status=s, retry_after=_retry_after(resp))
    if s in (401, 403):
        raise ProviderError("auth", body, status=s)
    if s >= 500 or s == 408 or s == 529:
        raise ProviderError("server", body, status=s, retry_after=_retry_after(resp))
    raise ProviderError("bad_request", body, status=s)


def _post(spec: ModelSpec, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict[str, Any]:
    client = _client(spec.base_url, headers.get("Authorization", "") + headers.get("x-api-key", ""))
    to = httpx.Timeout(timeout, connect=min(5.0, timeout))
    try:
        resp = client.post(url, json=body, headers=headers, timeout=to)
    except httpx.TimeoutException as e:
        raise ProviderError("timeout", type(e).__name__) from e
    except httpx.TransportError as e:
        raise ProviderError("connect", type(e).__name__ + ": " + str(e)[:120]) from e
    _raise_for_status(resp)
    try:
        return resp.json()
    except ValueError as e:
        raise ProviderError("server", "invalid json body", status=resp.status_code) from e


def kimi_params(spec: ModelSpec, tier: str) -> dict[str, Any]:
    """Moonshot quirks: k3 reasoning_effort, k2.x thinking off."""
    mid = spec.model.lower()
    vendor = (spec.vendor or "").lower()
    out: dict[str, Any] = {}
    if "kimi-k3" in mid:
        out["reasoning_effort"] = os.getenv("KIMI_K3_EFFORT", "low" if tier == "fast" else "high")
    elif vendor in ("kimi", "moonshot") or "kimi" in mid:
        if "kimi-k2" in mid:
            out["thinking"] = {"type": "disabled"}
    return out


def locks_temperature(spec: ModelSpec) -> bool:
    """Moonshot Kimi pins temperature (k2.6→0.6, k3→1); sending one errors or is ignored."""
    mid = spec.model.lower()
    vendor = (spec.vendor or "").lower()
    return vendor in ("kimi", "moonshot") or "kimi" in mid or "moonshot" in mid or "moonshot" in spec.base_url


def call_openai(
    spec: ModelSpec,
    *,
    api_key: str,
    system: str,
    user: str,
    tier: str,
    timeout: float,
    temperature: Optional[float],
    max_tokens: Optional[int],
    logprobs: bool = False,
) -> Completion:
    body: dict[str, Any] = {
        "model": spec.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if temperature is not None and not locks_temperature(spec):
        body["temperature"] = temperature
    mt = max_tokens or spec.max_tokens
    if mt:
        body["max_tokens"] = mt
    body.update(kimi_params(spec, tier))
    body.update(spec.params)
    if logprobs:
        body.update(logprobs=True, top_logprobs=20, max_tokens=1, temperature=0)
    data = _post(
        spec,
        spec.base_url + "/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        body,
        timeout,
    )
    try:
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        finish = str(choice.get("finish_reason") or "")
    except (AttributeError, IndexError, TypeError):
        content, finish = "", ""
    usage = data.get("usage") or {}
    return Completion(
        content=content,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        finish_reason=finish,
        logprobs=choice.get("logprobs") if logprobs else None,
    )


def call_anthropic(
    spec: ModelSpec,
    *,
    api_key: str,
    system: str,
    user: str,
    tier: str,
    timeout: float,
    temperature: Optional[float],
    max_tokens: Optional[int],
    logprobs: bool = False,
) -> Completion:
    if logprobs:
        raise ProviderError("unsupported", "logprobs unavailable")
    body: dict[str, Any] = {
        "model": spec.model,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "max_tokens": max_tokens or spec.max_tokens or 1024,
    }
    if temperature is not None:
        body["temperature"] = temperature
    body.update(spec.params)
    base = spec.base_url
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    data = _post(
        spec,
        url,
        {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        body,
        timeout,
    )
    parts = data.get("content") or []
    content = "".join(
        str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
    ).strip()
    usage = data.get("usage") or {}
    return Completion(
        content=content,
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
        finish_reason=str(data.get("stop_reason") or ""),
    )


def call(spec: ModelSpec, **kwargs: Any) -> Completion:
    if spec.protocol == PROTOCOL_ANTHROPIC:
        return call_anthropic(spec, **kwargs)
    return call_openai(spec, **kwargs)
