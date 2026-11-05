"""Administrative migration IO. Content access always requires a user scope."""
import json

from sqlalchemy import String, inspect, select
from sqlalchemy.orm import Mapped, mapped_column

from app import db
from app.data_access import IdentityRequired, current_user, system_scope
from app.data_access.memory_facts import MemoryFact, _owner
from app.data_access.session import memory_session
from app.gateway.embeddings import vector_blob


class MigrationReceipt(db.Base):
    __tablename__ = "memory_migration_receipts"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(256))
    fact_id: Mapped[int | None] = mapped_column(default=None)


def iter_user_ids(batch_size=100):
    """Admin-only keyset scan of owner IDs, never unscoped profile/event content."""
    from app.agent.memory.episodic import EpisodicEvent

    if current_user() is not None:
        raise IdentityRequired("Migration enumeration requires system scope")
    tables = set(inspect(db.ENGINE).get_table_names())
    models = [m for m in (db.UserProfile, EpisodicEvent) if m.__tablename__ in tables]
    after = ""
    while models:
        # Merge bounded owner-only pages without querying any user's content.
        with system_scope(), memory_session() as session:
            ids = sorted({uid for model in models for uid in session.scalars(
                select(model.user_id).where(model.user_id > after).distinct()
                .order_by(model.user_id).limit(batch_size)
            ) if uid.strip()})[:batch_size]
        if not ids:
            break
        yield from ids
        after = ids[-1]


def snapshot():
    """Read all legacy sources and historical facts for exactly one owner; no DDL."""
    from app.agent.memory.episodic import EpisodicEvent

    uid = _owner()
    tables = set(inspect(db.ENGINE).get_table_names())
    with memory_session() as session:
        row = session.get(db.UserProfile, uid) if "user_profiles" in tables else None
        profile = json.loads(row.profile_json or "{}") if row else {}
        events = []
        if "episodic_events" in tables:
            events = [{"id": row.id, "kind": row.kind, "summary": row.summary,
                       "thread_id": row.thread_id} for row in session.scalars(
                select(EpisodicEvent).where(EpisodicEvent.user_id == uid).order_by(EpisodicEvent.id))]
        facts = []
        if "memory_facts" in tables:
            facts = [{"id": row.id, "text": row.text} for row in session.scalars(
                select(MemoryFact).where(MemoryFact.user_id == uid))]
        receipts = {}
        if "memory_migration_receipts" in tables:
            receipts = {row.dedupe_key: row.fact_id for row in session.scalars(
                select(MigrationReceipt).where(MigrationReceipt.user_id == uid))}
    return {"profile": profile, "events": events, "facts": facts, "receipts": receipts}


def apply_batch(candidates, prepared, original, normalize):
    """Recheck under the shared write lock; facts and receipts commit together."""
    uid = _owner()
    with db.memory_transaction():
        current = snapshot()
        if any(current[key] != original[key] for key in ("profile", "events")):
            raise ValueError("Migration sources changed; retry this user")
        known = {normalize(fact["text"]): fact["id"] for fact in current["facts"]}
        added = 0
        with memory_session() as session:
            for candidate in candidates:
                if candidate.key in current["receipts"]:
                    continue
                normalized = normalize(candidate.text)
                fact_id = known.get(normalized)
                if fact_id is None:
                    embedded = prepared[candidate.key]
                    row = MemoryFact(user_id=uid, text=candidate.text, kind=candidate.kind,
                                     embedding=vector_blob(embedded.vector), embed_model=embedded.model,
                                     confirmed=candidate.confirmed, source_event_id=candidate.event_id,
                                     thread_id=candidate.thread_id)
                    session.add(row)
                    session.flush()
                    fact_id = row.id
                    known[normalized] = fact_id
                    added += 1
                session.add(MigrationReceipt(user_id=uid, dedupe_key=candidate.key,
                                             source=candidate.source, fact_id=fact_id))
            session.flush()
        return added
