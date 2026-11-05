"""Bounded hybrid reads; all candidate queries live in the scoped DAL."""
from concurrent.futures import ThreadPoolExecutor, wait
from html import escape
import json
import logging
import time

from app.agent.harness.leak import memory_safe
from app.agent.memory.redact import redact_text
from app.agent.token_meter import estimate_tokens
from app.agent.tool_registry import tool
from app.data_access import IdentityRequired, current_user, user_scope
from app.data_access import memory_facts
from app.data_access.session import memory_transaction_active
from app.feature_flags import memory_vector_enabled
from app.gateway import embeddings, reranking

log = logging.getLogger(__name__)
CHANNEL_TIMEOUT = 3.0
RERANK_TIMEOUT = 2.0
FACT_TOKEN_CAP = 600
HEADER = "【相关事实记忆】待确认条目不是用户确认的事实；只能询问核实，不得据此陈述用户经历或偏好。"


def rrf(channels):
    """Reciprocal rank fusion, equal channel weights and stable ID tie breaks."""
    scores, facts = {}, {}
    for channel in channels:
        seen = set()
        for rank, fact in enumerate(channel, 1):
            key = fact["id"]
            if key in seen:
                continue
            seen.add(key)
            facts[key] = fact
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
    return [facts[key] for key in sorted(scores, key=lambda key: (-scores[key], key))]


def _line(fact):
    label = "用户已确认" if fact["confirmed"] else "待确认（非用户确认，不可作为事实陈述）"
    # JSON quoting prevents embedded newlines from forging a confirmation label.
    return f"- [{label}] " + json.dumps({"kind": fact["kind"], "text": fact["text"]}, ensure_ascii=False)


def render_facts(facts):
    return HEADER + "\n" + "\n".join(_line(fact) for fact in facts) if facts else ""


def retrieve(query: str, *, kind: str | None = None, limit: int = 6,
             token_cap: int = FACT_TOKEN_CAP) -> list[dict]:
    if not memory_vector_enabled() or limit <= 0 or token_cap <= 0:
        return []
    uid = current_user()
    if uid is None:
        raise IdentityRequired("Memory retrieval requires a user scope")
    query = memory_safe(redact_text(query), uid)[:1000]

    def scoped(fn):
        # Install only the captured trusted identity, never a caller ID or a
        # request's SQLAlchemy transaction/session in these independent readers.
        with user_scope(uid):
            return fn()

    def embed_query():
        if not query.strip():
            return None
        return embeddings.embed_text(query, timeout=2.0)

    def vector_channel():
        embedded = embed_query()
        return memory_facts.knn(embedded.vector, 20, kind=kind) if embedded else []

    def available(fn):
        try:
            return fn()
        except Exception:
            log.warning("memory_retrieval_channel_unavailable")
            return []

    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="memory-read")
    try:
        keyword = lambda: memory_facts.keyword_search(query, 20, kind=kind)
        structured = lambda: memory_facts.structured_search(20, kind=kind)
        if memory_transaction_active():
            # SQLAlchemy sessions cannot be shared by concurrent readers. Keep
            # staged/transactional reads together while embedding runs remotely.
            deadline = time.monotonic() + CHANNEL_TIMEOUT
            future = pool.submit(scoped, embed_query)
            channels = [[], available(keyword), available(structured)]
            def staged_vector():
                embedded = future.result(timeout=max(0, deadline - time.monotonic()))
                return memory_facts.knn(embedded.vector, 20, kind=kind) if embedded else []
            channels[0] = available(staged_vector)
        else:
            futures = [pool.submit(scoped, fn) for fn in (vector_channel, keyword, structured)]
            done, _ = wait(futures, timeout=CHANNEL_TIMEOUT)
            channels = [available(future.result) if future in done else [] for future in futures]
        candidates = rrf(channels)
        # Expose only short, redacted public fields; never embeddings or owner IDs.
        candidates = [{"id": f["id"], "kind": memory_safe(redact_text(f["kind"]), uid),
                       "text": memory_safe(redact_text(f["text"]), uid),
                       "confirmed": bool(f["confirmed"])} for f in candidates]
        if candidates and query.strip():
            try:
                future = pool.submit(scoped, lambda: reranking.rerank(
                    query, [f["text"] for f in candidates], timeout=RERANK_TIMEOUT))
                order = future.result(timeout=RERANK_TIMEOUT)
                if (len(order) != len(candidates) or any(type(i) is not int for i in order)
                        or set(order) != set(range(len(candidates)))):
                    raise ValueError("invalid rerank order")
                candidates = [candidates[i] for i in order]
            except Exception:
                log.warning("memory_rerank_unavailable_using_rrf")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    selected = []
    for fact in candidates:
        if estimate_tokens(escape(render_facts(selected + [fact]), quote=False)) <= token_cap:
            selected.append(fact)
        if len(selected) >= min(limit, 10):
            break
    return selected


@tool("memory_facts_search", risk_level="read", description="查记忆：检索当前用户的有效事实，返回 confirmed 标记。",
      when_to_use="用户询问以前说过的偏好、计划、习惯或事情时查记忆。",
      when_not_to_use="不能搜索他人记忆；confirmed=false 只能核实，不得当成用户确认的事实。",
      tags=["memory"], timeout=7.0,
      params_schema={"type": "object", "properties": {
          "query": {"type": "string", "minLength": 1, "maxLength": 1000},
          "kind": {"type": ["string", "null"], "minLength": 1, "maxLength": 64},
          "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
          "required": ["query"], "additionalProperties": False})
def memory_facts_search(query: str, kind: str | None = None, limit: int = 6, *, user_id: str = "") -> list[dict]:
    """The registry replaces user_id from trusted context; retrieval uses only DAL scope."""
    return retrieve(query, kind=kind, limit=limit)
