"""Background vector writes: prepare outside the lock, apply with the summary.

UPDATE closes the old fact's validity interval and adds a replacement. Pending
facts are delivered to ``on_pending_fact`` only after the transaction commits.
No facts are retrieved into chat context here.
"""
from dataclasses import dataclass, replace
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import db
from app.agent import cost_ledger, token_meter
from app.agent.harness.leak import memory_safe, register_system_prompt
from app.agent.harness.redact import redact_text as redact_pii
from app.agent.memory.redact import redact_text
from app.data_access import NotFound, current_user
from app.data_access import memory_facts
from app.feature_flags import memory_vector_enabled
from app.gateway import embeddings, llm_gateway
from app.prompts import load_prompt

EXTRACT_SYSTEM = load_prompt("memory.extract", "v1")
RECONCILE_SYSTEM = load_prompt("memory.reconcile", "v1")
register_system_prompt("memory.extract", EXTRACT_SYSTEM)
register_system_prompt("memory.reconcile", RECONCILE_SYSTEM)


class Fact(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=400)
    source: Literal["user_stated", "ai_inferred"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @field_validator("kind", "text")
    @classmethod
    def short_statement(cls, value):
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("Expected a nonempty single-line fact")
        if re.search(r"[。！？!?]\s*\S|\.\s+\S", value.strip()):
            raise ValueError("Expected one sentence")
        return value.strip()


class Extraction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    facts: list[Fact] = Field(max_length=32)


class Reconciliation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["ADD", "UPDATE", "DELETE", "NOOP"]
    id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def target_required(self):
        if self.action in {"UPDATE", "DELETE"} and self.id is None:
            raise ValueError("Missing candidate id")
        if self.action == "ADD" and self.id is not None:
            raise ValueError("ADD cannot target an existing fact")
        return self


@dataclass(frozen=True)
class Decision:
    user_id: str
    fact: Fact
    embedding: embeddings.Embedding
    reconciliation: Reconciliation
    candidates: list[dict]


def safe_text(text, job):
    return redact_pii(memory_safe(redact_text(text), job.user_id))


def redacted_turn(job):
    """Redact before *any* background LLM call, including the legacy summary."""
    if current_user() != job.user_id:
        raise NotFound()
    return replace(job, kind=safe_text(job.kind, job), user_text=safe_text(job.user_text, job),
                   assistant_text=safe_text(job.assistant_text, job))


def _json_call(system, payload, task, schema):
    # Parse the literal response, without JSON repair or a valid fallback that
    # could accidentally turn a gateway failure into a memory decision.
    raw = llm_gateway.chat_text(system, json.dumps(payload, ensure_ascii=False),
                               task=task, fallback="INVALID", timeout=20.0)
    return schema.model_validate_json(raw)


def prepare_turn(job):
    """Return validated decisions without business writes; fail closed as a batch."""
    if not memory_vector_enabled():
        return []
    job = redacted_turn(job)
    if not job.user_text and not job.assistant_text:
        return []
    with token_meter.metered() as meter:
        try:
            return _prepare(job)
        finally:
            # Background workers have no node_wrap to charge the user's ledger.
            # Gateway usage and the meter retain memory_extract/reconcile labels.
            cost_ledger.record_spend(job.user_id, cost_ledger.cny_for_tokens(meter.tokens))


def _prepare(job):
    extracted = _json_call(EXTRACT_SYSTEM, {
        "user": job.user_text, "assistant": job.assistant_text,
    }, "memory_extract", Extraction)
    # Validate every fact before sending any of them to the embedding provider.
    for fact in extracted.facts:
        if safe_text(fact.text, job) != fact.text or safe_text(fact.kind, job) != fact.kind:
            raise ValueError("Unsafe extracted fact")
    decisions = []
    targets = set()
    for fact in extracted.facts:
        embedded = embeddings.embed_text(fact.text)
        if embedded is None:
            raise ValueError("Missing embedding")
        embeddings.vector_blob(embedded.vector)
        candidates = memory_facts.knn(embedded.vector, 5)
        decision = _json_call(RECONCILE_SYSTEM, {
            "fact": fact.model_dump(),
            "candidates": [{"id": c["id"], "kind": safe_text(c["kind"], job),
                            "text": safe_text(c["text"], job), "confirmed": c["confirmed"]}
                           for c in candidates],
        }, "memory_reconcile", Reconciliation)
        if decision.id is not None and decision.id not in {c["id"] for c in candidates}:
            raise ValueError("Reconciliation target is not a candidate")
        decision = _confirm_pending(fact, decision, candidates)
        if decision.action in {"UPDATE", "DELETE"}:
            if fact.source == "ai_inferred" and any(
                c["id"] == decision.id and c["confirmed"] for c in candidates
            ):
                raise ValueError("An inference cannot invalidate a confirmed fact")
            if decision.id in targets:
                raise ValueError("Conflicting decisions for one candidate")
            targets.add(decision.id)
        decisions.append(Decision(job.user_id, fact, embedded, decision, candidates))
    return decisions


def _confirm_pending(fact, decision, candidates):
    """A user statement confirms a duplicate inference, even if the LLM says NOOP."""
    if fact.source == "user_stated" and decision.action in {"ADD", "NOOP"}:
        for candidate in candidates:
            if candidate["confirmed"]:
                continue
            # A targeted NOOP identifies a semantic duplicate; without a target
            # only exact text is sufficient evidence to promote a candidate.
            matches = (candidate["id"] == decision.id if decision.id is not None
                       else candidate["text"] == fact.text)
            if matches:
                return Reconciliation(action="UPDATE", id=candidate["id"])
    return decision


def apply_decisions(decisions, *, thread_id="", source_event_id=None):
    """Join the summary transaction; exceptions roll back facts and summary alike."""
    if not memory_vector_enabled():
        return
    with db.memory_transaction():
        # Reads happened before acquiring the write lock. Reject stale snapshots
        # (including a concurrent forget/invalidate) before applying any decisions.
        for prepared in decisions:
            if current_user() != prepared.user_id:
                raise NotFound()
            for candidate in prepared.candidates:
                current = memory_facts.get_fact(candidate["id"])
                if (current is None or current["valid_until"] is not None
                        or current["updated_at"] != candidate["updated_at"]):
                    raise ValueError("Stale reconciliation candidates")
        for prepared in decisions:
            fact = prepared.fact
            decision = _confirm_pending(fact, prepared.reconciliation, prepared.candidates)
            if decision.action in {"UPDATE", "DELETE"}:
                if not memory_facts.invalidate_fact(decision.id):
                    raise ValueError("Could not invalidate candidate")
            if decision.action in {"ADD", "UPDATE"}:
                added = memory_facts.add_fact(
                    fact.text, kind=fact.kind, embedding=prepared.embedding.vector,
                    embed_model=prepared.embedding.model, confidence=fact.confidence,
                    confirmed=fact.source == "user_stated", thread_id=thread_id,
                    source_event_id=source_event_id,
                )
                if added is None:
                    raise ValueError("Could not add fact")
                if not added["confirmed"]:
                    db.after_memory_commit(lambda fact=added: on_pending_fact(fact))


def on_pending_fact(fact):
    """Post-commit subscription point, running under the turn owner's user_scope.

    The ask-first flow can subscribe via ask_cards.set_pending_fact_handler.
    Rollbacks never notify; handler failures are logged by after_memory_commit
    and do not retry an already committed batch.
    """
    from app.agent.ask_cards import on_pending_memory_fact

    on_pending_memory_fact(fact)
