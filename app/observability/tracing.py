"""OpenTelemetry spans for Langfuse: request → graph node → LLM generation / tool.

Design constraints:
- Off unless ``LANGFUSE_ENABLED`` + host + keys (see ``config``). When off, every
  helper is a cheap no-op, but a local trace id still exists per request so the
  ``X-Trace-Id`` header, trace rows and user feedback line up.
- Never blocks a request: spans go through a ``BatchSpanProcessor`` (bounded
  queue, background export thread; overflow drops spans instead of waiting).
- Never breaks a request: every public helper swallows its own errors.
- Redaction happens locally, twice: values are redacted when set (sandbox values of
  the current step, secrets, PII), and ``RedactingExporter`` scrubs every string
  attribute / event again on the export thread right before OTLP.
- User / thread ids are HMAC pseudonyms (``identity.user_pseudonym``).

Langfuse attribute mapping: https://langfuse.com/integrations/native/opentelemetry
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

from app.observability.config import LangfuseConfig, langfuse_config

log = logging.getLogger("observability.tracing")

_MAX_IO = 2000
_MAX_ATTR = 4000

# Keys whose values the service generates itself (ids, enums, model names); they are
# not scrubbed by the exporter so PII heuristics cannot mangle them.
_TRUSTED_KEYS = {
    "langfuse.observation.type",
    "langfuse.observation.level",
    "langfuse.user.id",
    "langfuse.session.id",
    "user.id",
    "session.id",
    "langfuse.trace.name",
    "langfuse.environment",
    "langfuse.release",
    "langfuse.version",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.system",
    "langfuse.observation.model.name",
    "langfuse.observation.prompt.name",
    "http.request.method",
    "url.path",
    "http.route",
}

_state: dict[str, Any] = {"key": None, "provider": None, "tracer": None, "error": "", "test": None}
_lock = threading.Lock()
_request: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar("obs_request", default=None)


# ---------------------------------------------------------------- redaction


def scrub(value: Any, *, extra: Sequence[str] = (), max_len: int = _MAX_ATTR) -> str:
    from app.agent.harness.redact import redact_text

    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return redact_text(s, max_len=max_len, extra_values=extra)


def _scrub_attrs(attrs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(attrs or {}).items():
        if k in _TRUSTED_KEYS:
            out[k] = v
        elif isinstance(v, str):
            out[k] = scrub(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [scrub(x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out


try:
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class RedactingExporter(SpanExporter):
        """Scrubs every span before it leaves the process. A span that cannot be
        scrubbed is dropped, never exported raw."""

        def __init__(self, inner: SpanExporter) -> None:
            self.inner = inner
            self.dropped = 0

        @staticmethod
        def redact_span(s: ReadableSpan) -> ReadableSpan:
            events = [
                Event(e.name, _scrub_attrs(e.attributes), timestamp=e.timestamp) for e in (s.events or ())
            ]
            return ReadableSpan(
                name=s.name,
                context=s.context,
                parent=s.parent,
                resource=s.resource,
                attributes=_scrub_attrs(s.attributes),
                events=events,
                links=s.links,
                kind=s.kind,
                status=s.status,
                start_time=s.start_time,
                end_time=s.end_time,
                instrumentation_scope=s.instrumentation_scope,
            )

        def export(self, spans: Sequence[ReadableSpan]) -> "SpanExportResult":
            clean = []
            for s in spans:
                try:
                    clean.append(self.redact_span(s))
                except Exception:
                    self.dropped += 1
            try:
                return self.inner.export(clean)
            except Exception as exc:
                log.warning("span export failed (%s)", type(exc).__name__)
                return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            try:
                self.inner.shutdown()
            except Exception:
                pass

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            try:
                return bool(self.inner.force_flush(timeout_millis))
            except Exception:
                return False

    _OTEL = True
except Exception:  # opentelemetry not installed
    _OTEL = False
    RedactingExporter = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------- provider


def _auth_header(cfg: LangfuseConfig) -> str:
    raw = f"{cfg.public_key}:{cfg.secret_key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _build(cfg: LangfuseConfig, exporter: Any = None, *, sync: bool = False) -> tuple[Any, Any]:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=cfg.otlp_endpoint,
            headers={"Authorization": _auth_header(cfg)},
            timeout=cfg.timeout_s,
        )
    resource = Resource.create(
        {
            "service.name": "personal-agent-service",
            "deployment.environment": cfg.environment,
            "service.version": cfg.release or "dev",
        }
    )
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(cfg.sample_rate)))
    wrapped = RedactingExporter(exporter)
    if sync:
        provider.add_span_processor(SimpleSpanProcessor(wrapped))
    else:
        provider.add_span_processor(
            BatchSpanProcessor(
                wrapped,
                max_queue_size=2048,
                schedule_delay_millis=1000,
                max_export_batch_size=256,
                export_timeout_millis=int(cfg.timeout_s * 1000),
            )
        )
    return provider, provider.get_tracer("personal-agent-service.harness")


def _tracer() -> Any:
    if not _OTEL:
        return None
    test = _state.get("test")
    if test is not None:
        return test["tracer"]
    cfg = langfuse_config()
    key = (cfg.enabled, cfg.host, cfg.public_key, cfg.secret_key, cfg.sample_rate, cfg.environment, cfg.release)
    if _state["key"] == key:
        return _state["tracer"]
    with _lock:
        if _state["key"] == key:
            return _state["tracer"]
        old = _state["provider"]
        provider = tracer = None
        err = ""
        if cfg.enabled:
            try:
                provider, tracer = _build(cfg)
            except Exception as exc:
                err = type(exc).__name__
                log.warning("Langfuse tracing disabled: %s", err)
        _state.update(key=key, provider=provider, tracer=tracer, error=err)
    if old is not None:
        threading.Thread(target=_shutdown_quietly, args=(old,), daemon=True).start()
    return tracer


def _shutdown_quietly(provider: Any) -> None:
    try:
        provider.shutdown()
    except Exception:
        pass


def use_test_exporter(exporter: Any, cfg: Optional[LangfuseConfig] = None) -> None:
    """Route spans synchronously to ``exporter`` (through RedactingExporter). Tests only."""
    from dataclasses import replace

    base = cfg or replace(langfuse_config(), enabled=True)
    provider, tracer = _build(base, exporter, sync=True)
    _state["test"] = {"provider": provider, "tracer": tracer, "cfg": base}


def clear_test_exporter() -> None:
    t = _state.get("test")
    _state["test"] = None
    if t:
        _shutdown_quietly(t["provider"])


def _cfg() -> LangfuseConfig:
    t = _state.get("test")
    return t["cfg"] if t else langfuse_config()


def enabled() -> bool:
    try:
        return _tracer() is not None
    except Exception:
        return False


def flush(timeout_s: float = 5.0) -> bool:
    t = _state.get("test")
    provider = t["provider"] if t else _state.get("provider")
    if provider is None:
        return True
    try:
        return bool(provider.force_flush(int(timeout_s * 1000)))
    except Exception:
        return False


def status() -> dict[str, Any]:
    cfg = _cfg()
    return {**cfg.public(), "active": enabled(), "otel_installed": _OTEL, "error": _state.get("error") or ""}


# ---------------------------------------------------------------- handles


def _as_attr(v: Any) -> Any:
    if isinstance(v, (str, bool, int, float)):
        return v
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False, default=str)[:_MAX_ATTR]


def _sandbox() -> list[str]:
    try:
        from app.agent.harness import context as trace_ctx

        ctx = trace_ctx.current() or {}
        return list((ctx.get("meta") or {}).get("sandbox") or [])
    except Exception:
        return []


class Observation:
    """Thin, failure-proof wrapper over one OTel span (``None`` span → no-op)."""

    def __init__(self, span: Any = None, *, trace_id: str = "") -> None:
        self.span = span
        self._trace_id = trace_id

    @property
    def recording(self) -> bool:
        try:
            return bool(self.span is not None and self.span.is_recording())
        except Exception:
            return False

    @property
    def trace_id(self) -> str:
        if self.span is not None:
            try:
                ctx = self.span.get_span_context()
                if ctx and ctx.trace_id:
                    return format(ctx.trace_id, "032x")
            except Exception:
                pass
        return self._trace_id

    @property
    def span_id(self) -> str:
        if self.span is not None:
            try:
                return format(self.span.get_span_context().span_id, "016x")
            except Exception:
                pass
        return ""

    def attr(self, key: str, value: Any) -> "Observation":
        if self.recording:
            try:
                self.span.set_attribute(key, _as_attr(value))
            except Exception:
                pass
        return self

    def attrs(self, values: dict[str, Any]) -> "Observation":
        for k, v in (values or {}).items():
            self.attr(k, v)
        return self

    def meta(self, **values: Any) -> "Observation":
        for k, v in values.items():
            if v is None:
                continue
            if isinstance(v, str):
                v = scrub(v, extra=_sandbox(), max_len=500)
            self.attr(f"langfuse.observation.metadata.{k}", v)
        return self

    def _io(self, key: str, value: Any) -> "Observation":
        if not self.recording or _cfg().export_io == "none" or value is None:
            return self
        try:
            self.attr(key, scrub(value, extra=_sandbox(), max_len=_MAX_IO))
        except Exception:
            pass
        return self

    def input(self, value: Any) -> "Observation":
        return self._io("langfuse.observation.input", value)

    def output(self, value: Any) -> "Observation":
        return self._io("langfuse.observation.output", value)

    def level(self, level: str, message: str = "") -> "Observation":
        self.attr("langfuse.observation.level", level)
        if message:
            self.attr("langfuse.observation.status_message", scrub(message, extra=_sandbox(), max_len=300))
        return self

    def event(self, name: str, attributes: Optional[dict[str, Any]] = None) -> "Observation":
        if self.recording:
            try:
                clean = {k: (scrub(v, extra=_sandbox(), max_len=500) if isinstance(v, str) else _as_attr(v))
                         for k, v in (attributes or {}).items()}
                self.span.add_event(name, clean)
            except Exception:
                pass
        return self


_NULL = Observation()


def _identity_attrs() -> dict[str, Any]:
    req = _request.get() or {}
    out: dict[str, Any] = {}
    if req.get("user"):
        out["langfuse.user.id"] = out["user.id"] = req["user"]
    if req.get("session"):
        out["langfuse.session.id"] = out["session.id"] = req["session"]
    return out


@contextmanager
def observe(name: str, *, kind: str = "span", input: Any = None, metadata: Optional[dict[str, Any]] = None) -> Iterator[Observation]:
    """Child span of whatever is current (request / node). Yields a no-op handle when off."""
    try:
        tracer = _tracer()
    except Exception:
        tracer = None
    if tracer is None:
        req = _request.get() or {}
        yield Observation(trace_id=str(req.get("trace_id") or ""))
        return
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    try:
        span = tracer.start_span(name)
        token = otel_context.attach(otel_trace.set_span_in_context(span))
    except Exception:
        yield _NULL
        return
    obs = Observation(span)
    obs.attr("langfuse.observation.type", kind).attrs(_identity_attrs())
    if input is not None:
        obs.input(input)
    if metadata:
        obs.meta(**metadata)
    try:
        yield obs
    except BaseException as exc:
        if type(exc).__name__ != "GraphInterrupt":
            obs.level("ERROR", type(exc).__name__)
        raise
    finally:
        try:
            otel_context.detach(token)
        except Exception:
            pass
        try:
            span.end()
        except Exception:
            pass


@contextmanager
def request_trace(name: str, *, method: str = "", path: str = "") -> Iterator[Observation]:
    """Root observation for one HTTP request; always yields a trace id."""
    state: dict[str, Any] = {"trace_id": uuid.uuid4().hex, "user": "", "session": "", "root": None}
    token = _request.set(state)
    try:
        with observe(name, kind="span") as obs:
            if obs.span is not None and obs.trace_id:
                state["trace_id"] = obs.trace_id
            if obs.recording:
                cfg = _cfg()
                state["root"] = obs
                obs.attrs(
                    {
                        "langfuse.trace.name": name,
                        "langfuse.environment": cfg.environment,
                        "http.request.method": method,
                        "url.path": path,
                    }
                )
                if cfg.release:
                    obs.attr("langfuse.release", cfg.release)
            else:
                obs._trace_id = state["trace_id"]
            yield obs
    finally:
        try:
            _request.reset(token)
        except Exception:
            pass


def set_identity(*, user_id: str = "", thread_id: str = "") -> None:
    """Attach pseudonymous user / session to the current trace (root + current span)."""
    try:
        from opentelemetry import trace as otel_trace

        from app.agent.harness.identity import thread_pseudonym, user_pseudonym

        req = _request.get()
        if req is None:
            return
        if user_id:
            req["user"] = user_pseudonym(user_id)
        if thread_id:
            req["session"] = thread_pseudonym(thread_id)
        attrs = _identity_attrs()
        root = req.get("root")
        if root is not None:
            root.attrs(attrs)
        Observation(otel_trace.get_current_span()).attrs(attrs)
    except Exception:
        pass


def current_ids() -> tuple[str, str]:
    """(trace_id, span_id) of the active span; local request trace id when tracing is off."""
    try:
        from opentelemetry import trace as otel_trace

        ctx = otel_trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    req = _request.get() or {}
    return str(req.get("trace_id") or ""), ""


def current() -> Observation:
    """Handle on the active span (no-op handle when tracing is off)."""
    try:
        from opentelemetry import trace as otel_trace

        return Observation(otel_trace.get_current_span())
    except Exception:
        return _NULL


def current_trace_id() -> str:
    return current_ids()[0]


def add_event(name: str, attributes: Optional[dict[str, Any]] = None) -> None:
    try:
        from opentelemetry import trace as otel_trace

        Observation(otel_trace.get_current_span()).event(name, attributes)
    except Exception:
        pass


def set_trace_io(*, input: Any = None, output: Any = None) -> None:
    """Trace-level input / output (what Langfuse evaluators see as {{input}} / {{output}})."""
    try:
        req = _request.get() or {}
        root = req.get("root")
        if root is None:
            return
        if input is not None and not req.get("io_input"):
            req["io_input"] = True
            root.input(input)
        if output is not None:
            root.output(output)
    except Exception:
        pass


def tag_trace(tags: Sequence[str]) -> None:
    req = _request.get()
    if req is None:
        return
    merged = list(dict.fromkeys([*(req.get("tags") or []), *(str(t) for t in tags)]))[:20]
    req["tags"] = merged
    root = req.get("root")
    if root is not None:
        root.attr("langfuse.trace.tags", merged)


def new_trace_id() -> str:
    return uuid.uuid4().hex
