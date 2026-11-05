"""Deterministic, resumable legacy memory backfill. Reports contain no content."""
from dataclasses import dataclass
import hashlib
from itertools import islice
import json
import logging
import re
import unicodedata

from app.agent.harness.redact import redact_text as redact_pii
from app.agent.memory.redact import display_text
from app.data_access import current_user, system_scope, user_scope
from app.data_access import memory_migration as store
from app.gateway import embeddings

log = logging.getLogger(__name__)
PROFILE_FIELDS = ("name", "city", "occupation", "interests", "preferences", "facts")


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).rstrip(".。;；!?！？")


def user_hash(uid):
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Candidate:
    key: str
    source: str
    text: str
    kind: str
    confirmed: bool
    event_id: int | None = None
    thread_id: str = ""


def candidates(snapshot):
    uid = current_user()
    profile = snapshot["profile"]
    seen = set()
    result = []
    skipped = 0

    def split(value, source, kind, confirmed, event_id=None, thread_id="", label=""):
        nonlocal skipped
        values = value if isinstance(value, list) else [value]
        for value in values:
            if not isinstance(value, str) or not value.strip():
                skipped += 1
                continue
            value = redact_pii(display_text(value))
            for part in re.split(r"[\n\r;；。!?！？]+|\.\s+", value):
                text = " ".join(part.strip(" \t-•").split())
                if not text:
                    continue
                text = label + text
                key = hashlib.sha256(json.dumps(
                    [uid, source, normalize(text)], ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                result.append(Candidate(key, source, text, kind, confirmed, event_id, thread_id))

    for field in PROFILE_FIELDS:
        if profile.get(field):
            split(profile[field], f"migration:profile:{field}", field, True, label=f"{field}: ")
    if profile.get("dialogue_summary"):
        split(profile["dialogue_summary"], "migration:profile:dialogue_summary", "summary", False)
    for event in snapshot["events"]:
        # Legacy event summaries lack user-stated provenance, even for profile_updated.
        split(event["summary"], f"migration:event:{event['id']}", "event", False,
              event["id"], event["thread_id"] or "")
    return result, skipped


def migrate_user(*, apply=False):
    """Migrate the trusted owner, including tombstones in the dedupe check."""
    original = store.snapshot()
    facts, skipped = candidates(original)
    known = {normalize(fact["text"]) for fact in original["facts"]}
    pending = []
    existing = 0
    for fact in facts:
        if fact.key in original["receipts"] or normalize(fact.text) in known:
            existing += 1
        else:
            pending.append(fact)
            known.add(normalize(fact.text))
    report = {"user_hash": user_hash(current_user()), "would_add": len(pending),
              "skip": skipped, "existing": existing, "added": 0, "failed": False}
    if apply:
        prepared = {fact.key: embeddings.embed_for_migration(fact.text) for fact in pending}
        if any(value is None for value in prepared.values()):
            raise ValueError("Missing migration embedding")
        report["added"] = store.apply_batch(facts, prepared, original, normalize)
    return report


def migrate_users(users=None, *, apply=False, limit=None):
    """Yield one sanitized report per user; failures leave that user's batch intact."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be nonnegative")
    if users is None:
        def owners():
            iterator = store.iter_user_ids()
            while True:
                with system_scope():
                    uid = next(iterator, None)
                if uid is None:
                    break
                yield uid
        users = owners()
    else:
        users = iter(dict.fromkeys(users))
    for uid in islice(users, limit):
        try:
            with user_scope(uid):
                report = migrate_user(apply=apply)
            yield report
        except Exception:
            # Exceptions may contain SQL parameters, fact text or provider payloads.
            log.error("memory migration failed user_hash=%s", user_hash(uid))
            yield {"user_hash": user_hash(uid), "would_add": 0, "skip": 0,
                   "existing": 0, "added": 0, "failed": True}
