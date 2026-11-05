"""Optional Langfuse / OpenTelemetry observability (off by default; see ``config``)."""

from app.observability.tracing import (  # noqa: F401
    add_event,
    current_ids,
    current_trace_id,
    enabled,
    observe,
    request_trace,
    set_identity,
    status,
)
