"""Phase B contracts: offline LLM/embedding fakes, real scoped transactions."""
import json
import threading

import pytest
from sqlalchemy import create_engine, event

from app import db
from app.agent import ask_cards, cost_ledger, token_meter
from app.agent.memory import episodic, profile, summary, write_path
from app.data_access import NotFound, current_user, user_scope
from app.data_access import memory_facts
from app.data_access.session import make_session_factory
from app.gateway import embeddings, llm_gateway


VECTOR = [1.0] + [0.0] * 1023
REAL_CHAT_TEXT = llm_gateway.chat_text


def fact(text="Prefers remote work.", source="user_stated", **kw):
    return {"kind": "preference", "text": text, "source": source, "confidence": 0.9, **kw}


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.extraction = {"facts": [fact()]}
        self.decisions = [{"action": "ADD"}]

    def __call__(self, system, user, *, task, **kw):
        self.calls.append((task, system, user, current_user(), threading.get_ident()))
        if task == "summary":
            return "Prefers remote work."
        assert db._memory_session.get() is None  # no network under the write lock
        out = self.extraction if task == "memory_extract" else self.decisions.pop(0)
        if isinstance(out, Exception):
            raise out
        return json.dumps(out) if not isinstance(out, str) else out


@pytest.fixture
def setup(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}",
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db, "SessionLocal", make_session_factory(engine))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    monkeypatch.setenv("PII_BACKEND", "regex")
    llm = FakeLLM()
    monkeypatch.setattr(llm_gateway, "chat_text", llm)
    monkeypatch.setattr(llm_gateway, "has_llm", lambda *a: True)
    embedded = []

    def embed(text):
        assert db._memory_session.get() is None
        embedded.append((text, current_user()))
        return embeddings.Embedding(VECTOR, "fake-embedding")

    monkeypatch.setattr(embeddings, "embed_text", embed)
    db.init_db()
    scheduler = summary.reset_summary_scheduler(retry_delay=0, max_attempts=1)
    scheduler.set_generator(lambda job: "Prefers remote work.")
    ask_cards.set_pending_fact_handler(None)
    yield llm, embedded, scheduler, engine
    scheduler.shutdown()
    ask_cards.set_pending_fact_handler(None)
    engine.dispose()


def job(**kw):
    return summary.SummaryJob(**{
        "user_id": "A", "kind": "chat", "user_text": "I prefer remote work.",
        "assistant_text": "Understood.", "thread_id": "thread-a", **kw,
    })


def active():
    return memory_facts.knn(VECTOR, 100)


def assert_no_memory():
    assert active() == []
    assert profile.get("A") == {}
    assert episodic.search_events("A") == []


@pytest.mark.parametrize("flag", [None, "0", "false"])
def test_flag_off_no_extra_calls_queries_or_fact_writes(setup, monkeypatch, flag):
    llm, embedded, scheduler, engine = setup
    if flag is None:
        monkeypatch.delenv("MEMORY_VECTOR_ENABLED")
    else:
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", flag)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    assert write_path.prepare_turn(job()) == []  # even without an identity
    assert scheduler._execute(job())["ok"]
    assert llm.calls == [] and embedded == []
    assert not any("INSERT INTO memory_facts" in sql or "FROM memory_facts" in sql
                   for sql in statements)


def test_extraction_add_and_owner_event_provenance(setup):
    llm, embedded, scheduler, _ = setup
    assert scheduler._execute(job())["ok"]
    with user_scope("A"):
        stored, = active()
        assert stored["text"] == fact()["text"]
        assert stored["confirmed"] is True
        assert stored["confidence"] == 0.9
        assert stored["thread_id"] == "thread-a"
        assert stored["embed_model"] == "fake-embedding"
        assert stored["source_event_id"] == episodic.search_events("A")[0]["id"]
    assert embedded == [(fact()["text"], "A")]
    assert [call[0] for call in llm.calls] == ["memory_extract", "memory_reconcile"]
    assert {call[3] for call in llm.calls} == {"A"}


def test_empty_extraction_has_no_embeddings_or_reconciliation(setup):
    llm, embedded, scheduler, _ = setup
    llm.extraction = {"facts": []}
    assert scheduler._execute(job())["ok"]
    assert embedded == [] and [c[0] for c in llm.calls] == ["memory_extract"]
    with user_scope("A"):
        assert active() == []
        assert profile.get("A")["dialogue_summary"]


@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_contradiction_invalidates_only_the_right_same_user_fact(setup, action):
    llm, _, scheduler, _ = setup
    with user_scope("B"):
        foreign = memory_facts.add_fact("Prefers office work.", embedding=VECTOR)
    with user_scope("A"):
        old = memory_facts.add_fact("Prefers office work.", embedding=VECTOR)
    llm.decisions = [{"action": action, "id": old["id"]}]
    assert scheduler._execute(job())["ok"]
    candidates = json.loads(llm.calls[1][2])["candidates"]
    assert [c["id"] for c in candidates] == [old["id"]]
    with user_scope("A"):
        assert memory_facts.get_fact(old["id"])["valid_until"] is not None
        rows = active()
        assert len(rows) == (1 if action == "UPDATE" else 0)
        if rows:
            assert rows[0]["id"] != old["id"] and rows[0]["text"] == fact()["text"]
    with user_scope("B"):
        assert memory_facts.get_fact(foreign["id"])["valid_until"] is None


@pytest.mark.parametrize("confirmed,source", [(True, "user_stated"), (False, "ai_inferred")])
def test_duplicate_noop(setup, confirmed, source):
    llm, _, scheduler, _ = setup
    with user_scope("A"):
        old = memory_facts.add_fact(fact()["text"], embedding=VECTOR, confirmed=confirmed)
    llm.extraction = {"facts": [fact(source=source)]}
    llm.decisions = [{"action": "NOOP", "id": old["id"]}]
    assert scheduler._execute(job())["ok"]
    with user_scope("A"):
        assert active()[0]["updated_at"] == old["updated_at"]
        assert len(active()) == 1


@pytest.mark.parametrize("action,targeted,text", [
    ("UPDATE", True, "Prefers remote work."),
    ("NOOP", True, "Wants to work remotely."),
    ("NOOP", False, "Prefers remote work."),
    ("ADD", False, "Prefers remote work."),
])
def test_user_statement_confirms_matching_pending_fact(setup, action, targeted, text):
    llm, _, scheduler, _ = setup
    notified = []
    ask_cards.set_pending_fact_handler(notified.append)
    llm.extraction = {"facts": [fact(source="ai_inferred")]}
    assert scheduler._execute(job())["ok"]
    with user_scope("A"):
        pending, = active()
        assert pending["confirmed"] is False
    llm.extraction = {"facts": [fact(text)]}
    llm.decisions = [{"action": action, **({"id": pending["id"]} if targeted else {})}]
    assert scheduler._execute(job(user_text="I want to work remotely."))["ok"]
    with user_scope("A"):
        confirmed, = active()
        assert confirmed["confirmed"] is True
        assert confirmed["text"] == text
        assert confirmed["id"] != pending["id"]
        assert memory_facts.get_fact(pending["id"])["valid_until"] is not None
    assert [row["id"] for row in notified] == [pending["id"]]


def test_apply_decisions_enforces_pending_confirmation(setup):
    with user_scope("A"):
        pending = memory_facts.add_fact("Wants to work remotely.", embedding=VECTOR)
        prepared = write_path.Decision(
            "A", write_path.Fact(**fact()), embeddings.Embedding(VECTOR, "fake"),
            write_path.Reconciliation(action="NOOP", id=pending["id"]), [pending],
        )
        write_path.apply_decisions([prepared])
        confirmed, = active()
        assert confirmed["confirmed"] is True
        assert confirmed["text"] == fact()["text"]
        assert memory_facts.get_fact(pending["id"])["valid_until"] is not None


@pytest.mark.parametrize("action", ["UPDATE", "DELETE", "NOOP"])
@pytest.mark.parametrize("target", ["foreign", "sixth", "invalidated", "invented"])
def test_ids_outside_top_five_rejected_with_no_writes(setup, action, target):
    llm, _, scheduler, _ = setup
    with user_scope("B"):
        foreign = memory_facts.add_fact("Foreign preference.", embedding=VECTOR)
    with user_scope("A"):
        rows = [memory_facts.add_fact(f"Preference {i}.", embedding=VECTOR) for i in range(6)]
        invalid = memory_facts.add_fact("Old preference.", embedding=VECTOR)
        memory_facts.invalidate_fact(invalid["id"])
        before = active()
    target_id = {"foreign": foreign["id"], "sixth": rows[5]["id"],
                 "invalidated": invalid["id"], "invented": 999999}[target]
    llm.decisions = [{"action": action, "id": target_id}]
    assert scheduler._execute(job())["ok"] is False
    assert len(json.loads(llm.calls[1][2])["candidates"]) == 5
    with user_scope("A"):
        assert active() == before
        assert profile.get("A") == {} and episodic.search_events("A") == []


def test_redaction_before_all_llm_prompts_including_prior_and_candidates(setup):
    llm, _, scheduler, _ = setup
    sensitive = "email: private@example.test phone: 13800138000 password: dummy-private-value"
    with user_scope("A"):
        profile.save("A", {"dialogue_summary": sensitive, "privacy_sandbox": ["hidden-project"]})
        memory_facts.add_fact(sensitive, embedding=VECTOR)
    scheduler.set_generator(None)
    result = scheduler._execute(job(user_text=f"{sensitive} hidden-project I prefer remote work.",
                                    assistant_text=sensitive))
    assert result["ok"]
    assert {c[0] for c in llm.calls} == {"summary", "memory_extract", "memory_reconcile"}
    for _, system, prompt, _, _ in llm.calls:
        for value in ("private@example.test", "13800138000", "dummy-private-value", "hidden-project"):
            assert value not in system + prompt


@pytest.mark.parametrize("invalid", [
    "not json", '```json\n{"facts": []}\n```', '{"facts": [',
    {"facts": [fact(confidence=1.1)]}, {"facts": [fact(confidence="0.9")]},
    {"facts": [fact(source="assistant")]}, {"facts": [fact(extra="not allowed")]},
    {"facts": [fact(text="First sentence. Second sentence.")]},
    {"facts": [fact(text="")]}, {"facts": [fact(text="\nresume\nbody")]},
    {"facts": [fact(), {"text": "Missing required fields."}]},
])
def test_invalid_extraction_zero_writes(setup, invalid):
    llm, embedded, scheduler, _ = setup
    llm.extraction = invalid
    assert scheduler._execute(job())["ok"] is False
    assert embedded == []
    with user_scope("A"):
        assert_no_memory()


@pytest.mark.parametrize("decision", ["invalid JSON", {"action": "REPLACE"},
                                       {"action": "UPDATE"}, {"action": "ADD", "id": 1},
                                       {"action": "DELETE", "id": "1"}])
def test_invalid_reconciliation_zero_writes(setup, decision):
    llm, _, scheduler, _ = setup
    llm.decisions = [decision]
    assert scheduler._execute(job())["ok"] is False
    with user_scope("A"):
        assert_no_memory()


@pytest.mark.parametrize("failure", ["embedding", "reconcile", "persistence"])
def test_mid_batch_failure_rolls_back_summary_facts_invalidations_and_hooks(setup, monkeypatch, failure):
    llm, embedded, scheduler, _ = setup
    with user_scope("A"):
        original = memory_facts.add_fact("Old preference.", embedding=VECTOR)
    llm.extraction = {"facts": [fact(source="ai_inferred"), fact("Prefers Python.")]}
    llm.decisions = [{"action": "UPDATE", "id": original["id"]}, {"action": "ADD"}]
    notified = []
    ask_cards.set_pending_fact_handler(notified.append)
    if failure == "embedding":
        embed = embeddings.embed_text

        def fail_embed(text):
            if embedded:
                raise RuntimeError("second embedding failed")
            return embed(text)

        monkeypatch.setattr(embeddings, "embed_text", fail_embed)
    elif failure == "reconcile":
        llm.decisions[1] = "bad JSON"
    else:
        add = memory_facts.add_fact
        calls = []

        def fail_add(*args, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("second write failed")
            return add(*args, **kw)

        monkeypatch.setattr(memory_facts, "add_fact", fail_add)
    assert scheduler._execute(job())["ok"] is False
    with user_scope("A"):
        assert memory_facts.get_fact(original["id"]) == original
        assert len(active()) == 1
        assert profile.get("A") == {} and episodic.search_events("A") == []
    assert notified == []


def test_confirmed_pending_and_adapter_after_commit(setup):
    llm, _, scheduler, _ = setup
    llm.extraction = {"facts": [fact(), fact("May prefer Python.", source="ai_inferred")]}
    llm.decisions = [{"action": "ADD"}, {"action": "ADD"}]
    notified = []

    def on_pending(row):
        assert current_user() == "A"
        assert db._memory_session.get() is None
        assert memory_facts.get_fact(row["id"])["confirmed"] is False
        notified.append(row)

    ask_cards.set_pending_fact_handler(on_pending)
    assert scheduler._execute(job())["ok"]
    with user_scope("A"):
        assert [row["confirmed"] for row in active()] == [True, False]
    assert len(notified) == 1 and notified[0]["text"] == "May prefer Python."


@pytest.mark.parametrize("handler_fails", [False, True])
def test_pending_adapter_noop_or_failure_does_not_retry_committed_batch(setup, handler_fails, caplog):
    llm, _, scheduler, _ = setup
    llm.extraction = {"facts": [fact(source="ai_inferred")]}
    if handler_fails:
        def fail_handler(row):
            raise RuntimeError("subscriber failed")

        ask_cards.set_pending_fact_handler(fail_handler)
    assert scheduler._execute(job())["ok"]
    with user_scope("A"):
        row, = active()
        assert row["confirmed"] is False
    assert len(llm.calls) == 2
    if handler_fails:
        assert "after-commit hook failed" in caplog.text


@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_inference_cannot_invalidate_confirmed_fact(setup, action):
    llm, _, scheduler, _ = setup
    with user_scope("A"):
        original = memory_facts.add_fact("Confirmed preference.", embedding=VECTOR, confirmed=True)
    llm.extraction = {"facts": [fact(source="ai_inferred")]}
    llm.decisions = [{"action": action, "id": original["id"]}]
    assert scheduler._execute(job())["ok"] is False
    with user_scope("A"):
        assert memory_facts.get_fact(original["id"]) == original


@pytest.mark.parametrize("text", [
    "password: dummy-private-value", "Email private@example.test.",
    "技能：Python\n工作经历：Example Corp\n教育背景：Example University",
])
def test_unsafe_extracted_facts_rejected_before_embedding(setup, text):
    llm, embedded, scheduler, _ = setup
    llm.extraction = {"facts": [fact(text)]}
    assert scheduler._execute(job())["ok"] is False
    assert embedded == []
    with user_scope("A"):
        assert_no_memory()


def test_sandbox_value_echo_rejected_even_when_it_fits_fact_schema(setup):
    llm, embedded, scheduler, _ = setup
    with user_scope("A"):
        profile.save("A", {"privacy_sandbox": ["hidden-project"]})
    llm.extraction = {"facts": [fact("Works on hidden-project.")]}
    assert scheduler._execute(job())["ok"] is False
    assert embedded == []
    with user_scope("A"):
        assert active() == []
        assert profile.get("A") == {"privacy_sandbox": ["hidden-project"]}
        assert episodic.search_events("A") == []


def test_stale_revision_drops_facts_and_notifications(setup, monkeypatch):
    llm, _, scheduler, _ = setup
    llm.extraction = {"facts": [fact(source="ai_inferred")]}
    notified = []
    ask_cards.set_pending_fact_handler(notified.append)
    original = embeddings.embed_text

    def forget_during_embedding(text):
        profile.save("A", {"memory_rev": 1})
        return original(text)

    monkeypatch.setattr(embeddings, "embed_text", forget_during_embedding)
    assert scheduler._execute(job())["dropped"]
    with user_scope("A"):
        assert active() == [] and episodic.search_events("A") == []
    assert notified == []


def test_prepared_batch_rejects_wrong_owner_and_stale_candidate(setup):
    llm, _, _, _ = setup
    with user_scope("A"):
        original = memory_facts.add_fact("Old preference.", embedding=VECTOR)
        llm.decisions = [{"action": "UPDATE", "id": original["id"]}]
        prepared = write_path.prepare_turn(job())
    with user_scope("B"), pytest.raises(NotFound):
        write_path.apply_decisions(prepared)
    with user_scope("A"):
        memory_facts.invalidate_fact(original["id"])
        with pytest.raises(ValueError, match="Stale"):
            write_path.apply_decisions(prepared)
        assert active() == []


def test_worker_keeps_every_turn_and_never_calls_llm_on_reply_thread(setup):
    llm, _, scheduler, _ = setup
    started, release = threading.Event(), threading.Event()
    caller = threading.get_ident()

    def generate(job):
        started.set()
        assert release.wait(3)
        return "Summary."

    scheduler.set_generator(generate)
    llm.decisions = [{"action": "ADD"}] * 3
    try:
        with user_scope("A"):
            summary.maybe_summarize_turn("A", kind="chat", user_text="Turn one.", assistant_text="OK")
            assert started.wait(2)
            for text in ("Turn two.", "Turn three."):
                summary.maybe_summarize_turn("A", kind="chat", user_text=text, assistant_text="OK")
        release.set()
        assert scheduler.flush(5)
        extracts = [c for c in llm.calls if c[0] == "memory_extract"]
        assert [json.loads(c[2])["user"] for c in extracts] == ["Turn one.", "Turn two.", "Turn three."]
        assert all(c[3] == "A" and c[4] != caller for c in llm.calls)
    finally:
        release.set()


@pytest.mark.parametrize("later_thread", ["thread-a", "thread-b"])
@pytest.mark.parametrize("max_attempts", [1, 2])
def test_failed_earlier_turn_cannot_override_later_preference(setup, monkeypatch, later_thread, max_attempts):
    _, _, scheduler, _ = setup
    scheduler.retry_delay = 0.05
    scheduler.max_attempts = max_attempts
    started, release = threading.Event(), threading.Event()
    earlier, later = "Prefers office work.", "Prefers remote work."
    attempts = []

    def chat_text(system, user, *, task, **kw):
        payload = json.loads(user)
        if task == "memory_extract":
            return json.dumps({"facts": [fact(payload["user"])]})
        assert task == "memory_reconcile"
        candidates = payload["candidates"]
        return json.dumps({"action": "UPDATE", "id": candidates[0]["id"]}
                          if candidates else {"action": "ADD"})

    def generate(turn):
        attempts.append((turn.user_text, turn.attempt))
        if turn.user_text == earlier and turn.attempt == 1:
            started.set()
            assert release.wait(3)
            raise RuntimeError("first turn initially fails")
        return turn.user_text

    monkeypatch.setattr(llm_gateway, "chat_text", chat_text)
    scheduler.set_generator(generate)
    try:
        with user_scope("A"):
            scheduler.submit(job(user_text=earlier))
            assert started.wait(2)
            scheduler.submit(job(user_text=later, thread_id=later_thread))
        release.set()
        assert scheduler.flush(5)
    finally:
        release.set()
    assert attempts == ([(earlier, 1)] + ([(earlier, 2)] if max_attempts == 2 else [])
                        + [(later, 1)])
    with user_scope("A"):
        latest, = active()
        assert latest["text"] == later
        assert latest["confirmed"] is True
        assert profile.get("A")["dialogue_summary"] == later


def test_waiting_retry_blocks_only_its_owners_later_turns(setup, monkeypatch):
    _, _, scheduler, _ = setup
    monkeypatch.setattr(scheduler, "_ensure_worker", lambda: None)
    monkeypatch.setattr(summary.time, "monotonic", lambda: 100.0)
    scheduler.retry_delay = 10
    scheduler.max_attempts = 2
    with user_scope("A"):
        scheduler.submit(job())
        scheduler.submit(job(thread_id="another-thread"))
    with user_scope("B"):
        scheduler.submit(job(user_id="B"))
    first_key = scheduler._ready_key()
    first = scheduler._pending.pop(first_key)
    scheduler._schedule_retry(first, first_key, "failed")
    other_key = scheduler._ready_key()
    assert scheduler._pending.pop(other_key).user_id == "B"
    assert scheduler._ready_key() is None
    monkeypatch.setattr(summary.time, "monotonic", lambda: 111.0)
    assert scheduler._ready_key() == first_key
    scheduler._pending.pop(first_key)
    assert scheduler._pending[scheduler._ready_key()].thread_id == "another-thread"


def test_gateway_fast_tasks_metered_and_charged_even_for_invalid_output(setup, monkeypatch):
    from app.gateway import core
    from app.llm import ModelTier

    # Exercise the real gateway, replacing only its upstream transport boundary.
    monkeypatch.setattr(llm_gateway, "chat_text", REAL_CHAT_TEXT)
    monkeypatch.setattr(embeddings, "embed_text", lambda _: embeddings.Embedding(VECTOR, "fake"))
    calls, charges = [], []

    def complete(system, user, **kw):
        calls.append(kw)
        return core.Result(content=json.dumps({"facts": [fact()]}) if len(calls) == 1 else "invalid")

    monkeypatch.setattr(core, "complete", complete)
    monkeypatch.setattr(cost_ledger, "record_spend", lambda uid, amount: charges.append((uid, amount)))
    with user_scope("A"), token_meter.metered() as meter:
        with pytest.raises(ValueError):
            write_path.prepare_turn(job())
        assert meter.calls == 2
        assert set(meter.by_task) == {"memory_extract", "memory_reconcile"}
        assert charges == [("A", cost_ledger.cny_for_tokens(meter.tokens))]
        assert_no_memory()
    assert [c["task"] for c in calls] == ["memory_extract", "memory_reconcile"]
    assert all(c["tier"] == "fast" for c in calls)
    assert llm_gateway.route("memory_extract") is ModelTier.FAST
    assert llm_gateway.route("memory_reconcile") is ModelTier.FAST
