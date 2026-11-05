"""Observability settings (all env-driven; everything is off unless explicitly enabled).

LANGFUSE_ENABLED       1/true to export traces and scores to Langfuse
LANGFUSE_HOST          e.g. https://langfuse.example.com (self-hosted)
LANGFUSE_PUBLIC_KEY    pk-lf-...
LANGFUSE_SECRET_KEY    sk-lf-...
LANGFUSE_ENVIRONMENT   trace environment (default APP_ENV or "development")
LANGFUSE_RELEASE       release tag (default GIT_SHA / RENDER_GIT_COMMIT)
LANGFUSE_SAMPLE_RATE   0..1 trace sampling (default 1.0)
LANGFUSE_EXPORT_IO     "redacted" (default): redacted, truncated input/output;
                       "none": metadata only (no message text leaves the server)
LANGFUSE_TIMEOUT_S     export / API timeout (default 5)
LANGFUSE_FEEDBACK_DATASET  dataset for "add to dataset" (default user-feedback)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.getenv(name) or default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class LangfuseConfig:
    enabled: bool
    host: str
    public_key: str
    secret_key: str
    environment: str
    release: str
    sample_rate: float
    export_io: str
    timeout_s: float
    feedback_dataset: str
    reason: str = ""

    @property
    def otlp_endpoint(self) -> str:
        return f"{self.host}/api/public/otel/v1/traces"

    def public(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "environment": self.environment,
            "release": self.release,
            "sample_rate": self.sample_rate,
            "export_io": self.export_io,
            "reason": self.reason,
        }


def langfuse_config() -> LangfuseConfig:
    host = (os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "").strip().rstrip("/")
    pk = (os.getenv("LANGFUSE_PUBLIC_KEY") or "").strip()
    sk = (os.getenv("LANGFUSE_SECRET_KEY") or "").strip()
    want = _truthy(os.getenv("LANGFUSE_ENABLED"))
    reason = ""
    if not want:
        reason = "LANGFUSE_ENABLED is off"
    elif not host.startswith(("http://", "https://")):
        reason = "LANGFUSE_HOST missing or not http(s)"
    elif not pk or not sk:
        reason = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY missing"
    io = (os.getenv("LANGFUSE_EXPORT_IO") or "redacted").strip().lower()
    return LangfuseConfig(
        enabled=want and not reason,
        host=host,
        public_key=pk,
        secret_key=sk,
        environment=(os.getenv("LANGFUSE_ENVIRONMENT") or os.getenv("APP_ENV") or "development").strip().lower()[:40],
        release=(os.getenv("LANGFUSE_RELEASE") or os.getenv("GIT_SHA") or os.getenv("RENDER_GIT_COMMIT") or "")[:40],
        sample_rate=_float("LANGFUSE_SAMPLE_RATE", 1.0, 0.0, 1.0),
        export_io=io if io in ("redacted", "none") else "redacted",
        timeout_s=_float("LANGFUSE_TIMEOUT_S", 5.0, 0.5, 60.0),
        feedback_dataset=(os.getenv("LANGFUSE_FEEDBACK_DATASET") or "user-feedback").strip(),
        reason=reason,
    )
