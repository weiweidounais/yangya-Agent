"""Minimal Langfuse public-API client (scores, datasets, dataset runs, prompts, traces).

Synchronous methods are for scripts (golden runner, eval CLI). ``submit`` queues a
call on a daemon thread for request paths (user feedback), so the request never
waits on Langfuse and a Langfuse outage never fails it.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Any, Callable, Optional

import httpx

from app.observability.config import LangfuseConfig, langfuse_config

log = logging.getLogger("observability.langfuse")


class LangfuseError(RuntimeError):
    pass


class LangfuseClient:
    def __init__(self, cfg: Optional[LangfuseConfig] = None, *, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.cfg = cfg or langfuse_config()
        self._transport = transport
        self._http: Optional[httpx.Client] = None

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.cfg.host,
                auth=(self.cfg.public_key, self.cfg.secret_key),
                timeout=self.cfg.timeout_s,
                transport=self._transport,
                headers={"User-Agent": "personal-agent-service-harness"},
            )
        return self._http

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        if not self.enabled:
            raise LangfuseError(f"langfuse disabled: {self.cfg.reason}")
        r = self._client().request(method, path, **kw)
        if r.status_code >= 400:
            raise LangfuseError(f"{method} {path} -> HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------ scores
    def create_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float | str,
        data_type: str = "NUMERIC",
        comment: str = "",
        observation_id: str = "",
        score_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": score_id or str(uuid.uuid4()),
            "traceId": trace_id,
            "name": name,
            "value": value,
            "dataType": data_type,
        }
        if comment:
            body["comment"] = comment[:1000]
        if observation_id:
            body["observationId"] = observation_id
        if metadata:
            body["metadata"] = metadata
        if self.cfg.environment:
            body["environment"] = self.cfg.environment
        return self._req("POST", "/api/public/scores", json=body)

    # ------------------------------------------------------------ datasets
    def create_dataset(self, name: str, *, description: str = "", metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._req(
            "POST", "/api/public/v2/datasets", json={"name": name, "description": description, "metadata": metadata or {}}
        )

    def create_dataset_item(
        self,
        *,
        dataset_name: str,
        input: Any,
        expected_output: Any = None,
        metadata: Optional[dict[str, Any]] = None,
        item_id: str = "",
        source_trace_id: str = "",
        source_observation_id: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"datasetName": dataset_name, "input": input, "metadata": metadata or {}}
        if expected_output is not None:
            body["expectedOutput"] = expected_output
        if item_id:
            body["id"] = item_id
        if source_trace_id:
            body["sourceTraceId"] = source_trace_id
        if source_observation_id:
            body["sourceObservationId"] = source_observation_id
        return self._req("POST", "/api/public/dataset-items", json=body)

    def create_dataset_run_item(
        self,
        *,
        run_name: str,
        dataset_item_id: str,
        trace_id: str,
        run_description: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "runName": run_name,
            "datasetItemId": dataset_item_id,
            "traceId": trace_id,
            "metadata": metadata or {},
        }
        if run_description:
            body["runDescription"] = run_description
        return self._req("POST", "/api/public/dataset-run-items", json=body)

    # ------------------------------------------------------------ prompts
    def create_prompt(
        self,
        *,
        name: str,
        prompt: str,
        labels: Optional[list[str]] = None,
        config: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
        commit_message: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "type": "text",
            "prompt": prompt,
            "labels": labels or [],
            "config": config or {},
            "tags": tags or [],
        }
        if commit_message:
            body["commitMessage"] = commit_message
        return self._req("POST", "/api/public/v2/prompts", json=body)

    # ------------------------------------------------------------ traces
    def list_traces(self, *, limit: int = 50, page: int = 1, **filters: Any) -> dict[str, Any]:
        params = {"limit": limit, "page": page, **{k: v for k, v in filters.items() if v not in (None, "")}}
        return self._req("GET", "/api/public/traces", params=params)

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        return self._req("GET", f"/api/public/traces/{trace_id}")

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None


# ---------------------------------------------------------------- background queue

_bg_lock = threading.Lock()
_bg: dict[str, Any] = {"q": None, "thread": None, "client": None, "key": None}
_bg_stats = {"submitted": 0, "done": 0, "failed": 0, "dropped": 0, "last_error": ""}


def _bg_client() -> LangfuseClient:
    cfg = langfuse_config()
    key = (cfg.host, cfg.public_key, cfg.secret_key, cfg.enabled)
    if _bg["client"] is None or _bg["key"] != key:
        _bg.update(client=LangfuseClient(cfg), key=key)
    return _bg["client"]


def _run() -> None:
    q: "queue.Queue[tuple[Callable[..., Any], dict[str, Any]]]" = _bg["q"]
    while True:
        fn, kw = q.get()
        try:
            fn(_bg_client(), **kw)
            _bg_stats["done"] += 1
        except Exception as exc:
            _bg_stats["failed"] += 1
            _bg_stats["last_error"] = type(exc).__name__
            log.warning("langfuse background call failed (%s)", type(exc).__name__)
        finally:
            q.task_done()


def submit(fn: Callable[..., Any], **kw: Any) -> bool:
    """Queue ``fn(client, **kw)``; False when Langfuse is off or the queue is full."""
    if not langfuse_config().enabled:
        return False
    with _bg_lock:
        if _bg["q"] is None:
            _bg["q"] = queue.Queue(maxsize=1000)
        if _bg["thread"] is None or not _bg["thread"].is_alive():
            _bg["thread"] = threading.Thread(target=_run, name="langfuse-bg", daemon=True)
            _bg["thread"].start()
    try:
        _bg["q"].put_nowait((fn, kw))
    except queue.Full:
        _bg_stats["dropped"] += 1
        return False
    _bg_stats["submitted"] += 1
    return True


def drain(timeout_s: float = 5.0) -> bool:
    q = _bg.get("q")
    if q is None:
        return True
    done = threading.Event()

    def _wait() -> None:
        q.join()
        done.set()

    threading.Thread(target=_wait, daemon=True).start()
    return done.wait(timeout_s)


def bg_stats() -> dict[str, Any]:
    q = _bg.get("q")
    return {**_bg_stats, "pending": q.qsize() if q is not None else 0}
