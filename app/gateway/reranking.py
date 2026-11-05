"""Opt-in SiliconFlow reranking through the gateway's pooled transport.

Wire contract: https://docs.siliconflow.cn/docs/api/rerank-post
Only validated input indices are returned; provider text is never trusted.
"""
import math
import time
import uuid

from app.feature_flags import memory_vector_enabled
from app.gateway import providers, usage
from app.gateway.config import get_config


def rerank(query: str, documents: list[str], *, timeout: float = 2.0) -> list[int]:
    if not memory_vector_enabled() or not documents or not query.strip():
        return list(range(len(documents)))
    spec = get_config().rerank_model("memory_rerank")
    start = time.monotonic()
    error = ""
    try:
        api_key = spec.api_key()
        if not api_key:
            raise providers.ProviderError("no_key", "memory_rerank is not configured")
        data = providers._post(
            spec, spec.base_url.rstrip("/") + "/rerank",
            {"Authorization": f"Bearer {api_key}"},
            {**spec.params, "model": spec.model, "query": query, "documents": documents,
             "top_n": len(documents), "return_documents": False},
            min(timeout, spec.timeout or timeout),
        )
        try:
            rows = data["results"]
            indices = [row["index"] for row in rows]
            if (len(indices) != len(documents) or any(type(i) is not int for i in indices)
                    or set(indices) != set(range(len(documents)))):
                raise ValueError("invalid indices")
            if any(type(row["relevance_score"]) not in (int, float)
                   or not math.isfinite(row["relevance_score"]) for row in rows):
                raise ValueError("invalid scores")
            return [row["index"] for row in sorted(rows, key=lambda r: -r["relevance_score"])]
        except (KeyError, TypeError, ValueError) as exc:
            raise providers.ProviderError("server", "invalid memory rerank response") from exc
    except Exception as exc:
        error = exc.kind if isinstance(exc, providers.ProviderError) else "error"
        raise
    finally:
        usage.record(request_id=uuid.uuid4().hex[:12], task="memory_rerank", tier="rerank",
                     spec=spec, attempt=0, ok=not error, error=error,
                     latency_ms=(time.monotonic() - start) * 1000)
