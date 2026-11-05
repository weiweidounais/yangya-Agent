"""Opt-in memory embeddings using the gateway registry and pooled transport."""
from dataclasses import dataclass
import math
import struct
from typing import Sequence

from app.feature_flags import memory_vector_enabled
from app.gateway import providers
from app.gateway.config import get_config

DIMENSIONS = 1024
DEFAULT_MODEL = "BAAI/bge-m3"


def vector_blob(vector: Sequence[float]) -> bytes:
    """Validate and encode the float32 representation consumed by sqlite-vec."""
    if len(vector) != DIMENSIONS:
        raise ValueError("Memory embeddings must have 1024 dimensions")
    try:
        blob = struct.pack(f"<{DIMENSIONS}f", *vector)
    except (OverflowError, TypeError, struct.error) as exc:
        raise ValueError("Memory embeddings must contain finite float32 values") from exc
    values = struct.unpack(f"<{DIMENSIONS}f", blob)
    if not all(math.isfinite(v) for v in values) or not any(values):
        raise ValueError("Memory embeddings must be finite and nonzero")
    return blob


@dataclass(frozen=True)
class Embedding:
    vector: Sequence[float]
    model: str


def embed_text(text: str, *, timeout: float | None = None) -> Embedding | None:
    """Return None without configuration/HTTP access while the flag is off."""
    if not memory_vector_enabled():
        return None
    return _embed_text(text, timeout=timeout)


def embed_for_migration(text: str) -> Embedding:
    """Explicit administrative backfill; independent of live rollout settings."""
    from app.data_access import current_user, IdentityRequired
    if current_user() is None:
        raise IdentityRequired("Migration embeddings require a user scope")
    return _embed_text(text)


def _embed_text(text: str, *, timeout: float | None = None) -> Embedding:
    spec = get_config().embedding_model("memory_embed")
    api_key = spec.api_key()
    if not api_key:
        raise providers.ProviderError("no_key", "memory_embed is not configured")
    data = providers._post(
        spec, spec.base_url.rstrip("/") + "/embeddings",
        {"Authorization": f"Bearer {api_key}"},
        {**spec.params, "model": spec.model, "input": text, "encoding_format": "float"},
        timeout if timeout is not None else spec.timeout or 20.0,
    )
    try:
        vector = data["data"][0]["embedding"]
        vector_blob(vector)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise providers.ProviderError("server", "invalid memory embedding response") from exc
    return Embedding(vector=vector, model=spec.model)
