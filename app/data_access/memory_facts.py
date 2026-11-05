"""User-scoped vector facts and hybrid retrieval channels.

Vectors live on the fact row, so deletion and transaction rollback cannot leave
an orphaned vector index. sqlite-vec performs exact cosine top-k on the scoped
active rows; installations without loadable extensions use Python cosine.
"""
from datetime import datetime, timezone
import logging
import math
import re
import sqlite3
import struct
import threading

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, LargeBinary, String, Text, case, delete, func, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from app.data_access import IdentityRequired, current_user
from app.data_access.session import memory_session
from app.db import Base
from app.feature_flags import memory_vector_enabled
from app.gateway import embeddings

log = logging.getLogger(__name__)
_fallback_logged = False
_fallback_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    __table_args__ = (CheckConstraint("length(embedding) = 4096", name="memory_embedding_1024"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), default="fact")
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    source_event_id: Mapped[int | None] = mapped_column(default=None)
    thread_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    embed_model: Mapped[str] = mapped_column(String(128))


def _owner():
    uid = current_user()
    if uid is None:
        raise IdentityRequired("Vector memory requires a user scope")
    return uid


def _fact(row):
    result = {column.name: getattr(row, column.name) for column in MemoryFact.__table__.columns}
    # SQLite drops timezone information on reads; keep the public values UTC
    # whether the row was just flushed or loaded in a later session.
    for name in ("created_at", "updated_at", "valid_until"):
        value = result[name]
        if value is not None and value.tzinfo is None:
            result[name] = value.replace(tzinfo=timezone.utc)
    return result


def add_fact(text, *, kind="fact", embedding=None, source_event_id=None,
             thread_id="", confidence=1.0, confirmed=False,
             embed_model=embeddings.DEFAULT_MODEL, user_id=None):
    """Caller-supplied user_id is ignored; only the trusted scope owns the row."""
    if not memory_vector_enabled():
        return None
    uid = _owner()
    if embedding is None:
        result = embeddings.embed_text(text)
        if result is None:
            return None
        embedding, embed_model = result.vector, result.model
    blob = embeddings.vector_blob(embedding)
    with memory_session() as session:
        row = MemoryFact(user_id=uid, kind=kind, text=text, embedding=blob,
                         source_event_id=source_event_id, thread_id=thread_id,
                         confidence=confidence, confirmed=confirmed, embed_model=embed_model)
        session.add(row)
        session.flush()
        return _fact(row)


def get_fact(fact_id, *, user_id=None):
    if not memory_vector_enabled():
        return None
    uid = _owner()
    with memory_session() as session:
        row = session.scalar(select(MemoryFact).where(MemoryFact.id == fact_id, MemoryFact.user_id == uid))
        return _fact(row) if row else None


def invalidate_fact(fact_id, *, user_id=None):
    if not memory_vector_enabled():
        return False
    uid = _owner()
    with memory_session() as session:
        row = session.scalar(select(MemoryFact).where(MemoryFact.id == fact_id, MemoryFact.user_id == uid))
        if row is None:
            return False
        row.valid_until = row.updated_at = _now()
        session.flush()
        return True


def delete_fact(fact_id, *, user_id=None):
    if not memory_vector_enabled():
        return False
    uid = _owner()
    with memory_session() as session:
        result = session.execute(delete(MemoryFact).where(MemoryFact.id == fact_id, MemoryFact.user_id == uid))
        return bool(result.rowcount)


def forget_all(*, user_id=None):
    if not memory_vector_enabled():
        return 0
    uid = _owner()
    with memory_session() as session:
        result = session.execute(delete(MemoryFact).where(MemoryFact.user_id == uid))
        return result.rowcount


def _load_sqlite_vec(connection):
    import sqlite_vec

    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


def _has_sqlite_vec(session):
    global _fallback_logged
    connection = session.connection()
    if "memory_sqlite_vec" not in connection.info:
        try:
            _load_sqlite_vec(connection.connection.driver_connection)
        except (ImportError, AttributeError, OSError, sqlite3.Error):
            connection.info["memory_sqlite_vec"] = False
            with _fallback_lock:
                if not _fallback_logged:
                    log.warning("sqlite-vec unavailable; memory search uses user-scoped Python cosine")
                    _fallback_logged = True
        else:
            connection.info["memory_sqlite_vec"] = True
    return connection.info["memory_sqlite_vec"]


def _cosine_distance(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return 1.0 - max(-1.0, min(1.0, dot / norm))


def knn(query_vec, k, *, kind=None, user_id=None):
    """Exact top-k, with owner and validity predicates before ranking/limiting."""
    if not memory_vector_enabled():
        return []
    uid = _owner()
    if k <= 0:
        return []
    blob = embeddings.vector_blob(query_vec)
    predicates = (MemoryFact.user_id == uid, MemoryFact.valid_until.is_(None))
    if kind is not None:
        predicates += (MemoryFact.kind == kind,)
    with memory_session() as session:
        if _has_sqlite_vec(session):
            distance = func.vec_distance_cosine(MemoryFact.embedding, blob).label("distance")
            rows = session.execute(select(MemoryFact, distance).where(*predicates)
                                   .order_by(distance, MemoryFact.id).limit(k)).all()
        else:
            query = struct.unpack("<1024f", blob)
            facts = session.scalars(select(MemoryFact).where(*predicates)).all()
            rows = sorted(
                ((row, _cosine_distance(query, struct.unpack("<1024f", row.embedding))) for row in facts),
                key=lambda pair: (pair[1], pair[0].id),
            )[:k]
        return [{**_fact(row), "distance": float(distance)} for row, distance in rows]


PROFILE_KINDS = ("preference", "interest", "goal", "plan", "habit", "relationship",
                 "city", "occupation", "fact", "偏好", "兴趣", "目标", "计划", "习惯", "备注")


def keyword_search(query, k=20, *, kind=None, user_id=None):
    """Literal LIKE terms, ranked by overlap; scope and validity precede LIMIT."""
    if not memory_vector_enabled():
        return []
    uid = _owner()
    terms = set()
    for word in re.findall(r"[a-z0-9_+#.-]+|[\u3400-\u9fff]+", query.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 1:
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        elif len(word) > 1:
            terms.add(word)
    if not terms or k <= 0:
        return []
    matches = [MemoryFact.text.icontains(term, autoescape=True) for term in sorted(terms)[:32]]
    score = sum(case((match, 1), else_=0) for match in matches)
    query = select(MemoryFact).where(MemoryFact.user_id == uid,
                                    MemoryFact.valid_until.is_(None), or_(*matches))
    if kind is not None:
        query = query.where(MemoryFact.kind == kind)
    with memory_session() as session:
        rows = session.scalars(query.order_by(score.desc(), MemoryFact.id.desc()).limit(k)).all()
        return [_fact(row) for row in rows]


def structured_search(k=20, *, kind=None, user_id=None):
    """Profile-linked kinds, or an explicitly requested kind, for this user only."""
    if not memory_vector_enabled():
        return []
    uid = _owner()
    if k <= 0:
        return []
    predicate = MemoryFact.kind == kind if kind is not None else MemoryFact.kind.in_(PROFILE_KINDS)
    with memory_session() as session:
        rows = session.scalars(select(MemoryFact).where(
            MemoryFact.user_id == uid, MemoryFact.valid_until.is_(None), predicate,
        ).order_by(MemoryFact.updated_at.desc(), MemoryFact.id.desc()).limit(k)).all()
        return [_fact(row) for row in rows]
