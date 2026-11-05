"""Retrieval and rolling compression use an offline gateway override."""
import json
from dataclasses import replace

import pytest

from app.agent.context_assembler import (
    ContextPolicy, assemble_context, conversation_state, memory_context, record_turn,
)
from app.agent.memory import episodic, profile
from app.agent.token_meter import estimate_tokens
from app.gateway import llm_gateway


@pytest.mark.parametrize("section", ["draft", "profile", "turns", "summary"])
def test_profile_update_preserves_sandbox_for_context_and_output(uid, monkeypatch, section):
    from app import db
    from app.agent.harness.leak import SANDBOX_PLACEHOLDER, public_profile, scan_output
    from app.agent.subgraphs.profile import load_profile, node_update_profile

    private = "Synthetic-Private-Project-99"
    monkeypatch.setattr(llm_gateway, "has_llm", lambda *args: False)
    node_update_profile({
        "user_id": uid,
        "profile": {"name": "示例用户", "privacy_sandbox": [private]},
    })
    stored = db.get_profile(uid)
    assert stored["privacy_sandbox"] == [private]
    assert stored["name"] == "示例用户"
    state = {"user_id": uid, "thread_id": "privacy-context"}
    if section == "draft":
        state["draft"] = {"kind": "email", "title": "请假邮件", "body": private}
    elif section == "profile":
        db.merge_and_save(uid, {"facts": [private]})
    else:
        saved = conversation_state(state)
        if section == "turns":
            saved["turns"] = [{"user": private, "assistant": private}]
        else:
            saved["summary"] = private
        state["conversation_context"] = saved

    context = assemble_context(state, query="")
    assert private not in context
    assert SANDBOX_PLACEHOLDER in context
    loaded = load_profile(state)
    assert loaded["privacy_sandbox"] == [private]
    assert private not in json.dumps(public_profile(loaded, user_id=uid))
    assert scan_output(private, user_id=uid, profile=loaded)["text"] == SANDBOX_PLACEHOLDER


@pytest.fixture
def gateway():
    calls = []
    def override(kind, system, user, tier, fallback):
        # Routing also uses the gateway; only compression advances this counter.
        if system.startswith("Select one tool"):
            return {"tool": None, "confidence": 1.0}
        calls.append((kind, system, user))
        return {"summary": f"rolling-{len(calls)}"}
    previous = llm_gateway.get_llm_override()
    llm_gateway.set_llm_override(override)
    yield calls
    llm_gateway.set_llm_override(previous)


def test_rolling_summary_only_receives_evicted_turns(uid, gateway):
    state = {"user_id": uid, "thread_id": "one"}
    policy = ContextPolicy(raw_turns=2)
    for i in range(4):
        before = json.dumps(state)
        saved = record_turn(state, f"user-{i}", f"reply-{i}", policy=policy)
        assert json.dumps(state) == before
        state["conversation_context"] = saved
    assert len(gateway) == 2
    assert "user-0" in gateway[0][2] and "user-1" not in gateway[0][2]
    assert "rolling-1" in gateway[1][2] and "user-1" in gateway[1][2]
    assert "user-0" not in gateway[1][2]
    assert [t["user"] for t in saved["turns"]] == ["user-2", "user-3"]
    context = assemble_context(state, query="", policy=policy)
    assert "rolling-2" in context and "user-2" in context and "reply-3" in context


@pytest.mark.parametrize("budget", [0, 1, 20, 100, 500])
def test_rendered_token_budget_and_markup(uid, budget):
    state = {"user_id": uid, "thread_id": "one"}
    state["conversation_context"] = record_turn(state, "中文<&" * 400, "answer" * 400)
    out = assemble_context(state, query="", policy=ContextPolicy(token_budget=budget))
    assert estimate_tokens(out) <= budget
    if out:
        assert out.startswith("<memory>\n") and out.endswith("\n</memory>")
        assert out.count("<memory>") == 1


def test_failed_summary_is_bounded_and_crisis_skips_gateway(uid, monkeypatch, gateway):
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    monkeypatch.setattr(llm_gateway, "chat_json", fail)
    state = {"user_id": uid}
    policy = ContextPolicy(raw_turns=0, summary_tokens=20)
    saved = record_turn(state, "Python " * 100, "reply", policy=policy)
    assert saved["turns"] == []
    assert 0 < estimate_tokens(saved["summary"]) <= 20
    monkeypatch.setattr(llm_gateway, "chat_json", lambda *a, **kw: pytest.fail("crisis compression"))
    record_turn(state, "help", "reply", policy=policy, compress=False)


def test_isolation_and_memory_revision_invalidate_context(uid):
    state = {"user_id": uid, "thread_id": "one"}
    state["conversation_context"] = record_turn(state, "old fact", "old reply")
    for change in ({"thread_id": "two"}, {"user_id": uid + "other"}):
        assert conversation_state({**state, **change})["turns"] == []
    profile.merge(uid, {"memory_rev": 1})
    assert conversation_state(state)["turns"] == []
    assert "old fact" not in assemble_context(state, query="")


@pytest.mark.parametrize("identity", [{}, {"user_id": None}, {"user_id": ""}, {"user_id": "   "}])
def test_missing_identity_rejected_before_memory_access(identity, monkeypatch):
    monkeypatch.setattr(profile, "get", lambda *a, **kw: pytest.fail("unexpected profile lookup"))
    state = {**identity, "conversation_context": {
        "user_id": "demo", "thread_id": "", "memory_rev": 0,
        "summary": "another user's summary", "turns": [{"user": "private", "assistant": "reply"}],
    }}
    with pytest.raises(ValueError, match="requires user_id"):
        conversation_state(state)
    with pytest.raises(ValueError, match="requires user_id"):
        assemble_context(state, query="Python")
    with pytest.raises(ValueError, match="requires user_id"):
        record_turn(state, "hello", "reply")


def test_secrets_removed_before_compression(uid, gateway):
    state = {"user_id": uid}
    saved = record_turn(state, "Python password: secret123", "ok",
                        policy=ContextPolicy(raw_turns=0))
    assert "secret123" not in json.dumps(saved)
    assert "secret123" not in gateway[0][2]


def test_retrieval_relevance_recency_old_hits_and_user_scope(uid):
    old = episodic.write_event(uid, "chat", "Python Redis backend")
    for _ in range(25):
        episodic.write_event(uid, "chat", "unrelated")
    newer = episodic.write_event(uid, "chat", "Python Redis remote")
    partial = episodic.write_event(uid, "chat", "Python only")
    from app.data_access import user_scope
    with user_scope(uid + "other"):
        episodic.write_event(uid + "other", "chat", "Python Redis private")
    events = episodic.retrieve_events(uid, query="help with Python Redis", limit=3)
    assert [e["id"] for e in events] == [newer["id"], old["id"], partial["id"]]
    assert "private" not in memory_context(uid, query="Python Redis")


def test_chinese_tags_and_literal_wildcards(uid):
    event = episodic.write_event(uid, "chat", "希望上海远程工作", tags=["remote"])
    assert episodic.retrieve_events(uid, query="上海的机会")[0]["id"] == event["id"]
    assert episodic.retrieve_events(uid, tags=["remote"])[0]["id"] == event["id"]
    episodic.write_event(uid, "chat", "abcXdef")
    assert episodic.retrieve_events(uid, query="abc_def") == []


def test_no_demand_does_not_search(uid, monkeypatch):
    monkeypatch.setattr(episodic, "retrieve_events", lambda *a, **kw: pytest.fail("unexpected lookup"))
    memory_context(uid)
    memory_context(uid, query="Python", limit=0)
    memory_context(uid, query="Python", token_budget=0)


def test_policy_rejects_negative_limits():
    with pytest.raises(ValueError):
        replace(ContextPolicy(), raw_turns=-1)


def test_budget_retains_newest_turn_first(uid):
    state = {"user_id": uid}
    state["conversation_context"] = record_turn(state, "OLD " * 300, "old answer")
    state["conversation_context"] = record_turn(state, "LATEST request", "new answer")
    out = assemble_context(state, query="", policy=ContextPolicy(token_budget=80))
    assert "LATEST request" in out and "new answer" in out


def test_chat_uses_and_updates_conversation(uid, monkeypatch, fake_llm):
    from app.agent.subgraphs import chat

    monkeypatch.setattr(chat, "maybe_summarize_turn", lambda *a, **kw: None)
    monkeypatch.setattr(chat, "_llm_risk", lambda msg: False)
    state = {"user_id": uid, "thread_id": "chat-test", "message": "今天想聊聊电影"}
    first = chat.node_chat(state)
    assert first["conversation_context"]["turns"][0]["user"] == state["message"]
    fake_llm.calls.clear()
    chat.node_chat({**state, **first, "message": "接着聊吧"})
    assert "今天想聊聊电影" in fake_llm.calls[-1][0]


def test_graph_checkpoint_rolls_conversation(uid, monkeypatch, gateway, fake_llm):
    from app.agent.subgraphs import chat
    from app.graph import get_thread_snapshot, run_action

    monkeypatch.setattr(chat, "maybe_summarize_turn", lambda *a, **kw: None)
    monkeypatch.setattr(chat, "_llm_risk", lambda msg: False)
    tid = "context-" + uid
    for i in range(6):
        run_action({"user_id": uid, "action": "chat", "message": f"今天想聊聊电影 {i}"}, thread_id=tid)
    saved = get_thread_snapshot(tid)["values"]["conversation_context"]
    assert len(saved["turns"]) == 4
    assert saved["turns"][0]["user"].endswith("2")
    assert saved["turns"][-1]["user"].endswith("5")
    assert saved["summary"] == "rolling-2"
    assert len(gateway) == 2


def test_offline_summary_keeps_newly_evicted_material(uid, monkeypatch):
    monkeypatch.setattr(llm_gateway, "has_llm", lambda: False)
    policy = ContextPolicy(raw_turns=0, summary_tokens=30)
    state = {"user_id": uid}
    state["conversation_context"] = record_turn(state, "OLD " * 200, "ok", policy=policy)
    saved = record_turn(state, "NEW preference", "ok", policy=policy)
    assert "NEW preference" in saved["summary"]
