"""Phase C offline retrieval, ownership, context, gateway and A/B contracts."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event

from app import db
from app.agent import context_assembler, tool_registry
from app.agent.memory import episodic, profile, retrieval
from app.agent.token_meter import estimate_tokens
from app.data_access import IdentityRequired, memory_facts as facts, system_scope, user_scope
from app.data_access.session import make_session_factory
from app.gateway import embeddings, providers, reranking
from app.gateway.config import ConfigError, get_config


VECTOR = [1.0] + [0.0] * 1023


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/retrieval.db")
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db, "SessionLocal", make_session_factory(engine))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    monkeypatch.setenv("TOOL_TRACE_READ_SAMPLE", "0")
    monkeypatch.setattr(embeddings, "embed_text", lambda text, **kw: embeddings.Embedding(VECTOR, embeddings.DEFAULT_MODEL))
    monkeypatch.setattr(reranking, "rerank", lambda query, documents, **kw: list(range(len(documents))))
    db.init_db()
    yield engine
    engine.dispose()


def seed():
    with user_scope("B"):
        foreign = facts.add_fact("Python B private", kind="preference", confirmed=True)
    with user_scope("A"):
        own = facts.add_fact("Python A pending", kind="preference")
        old = facts.add_fact("Python A invalid", kind="preference", confirmed=True)
        facts.invalidate_fact(old["id"])
    return own, foreign, old


@pytest.mark.parametrize("channel", ["vector", "keyword", "structured"])
def test_channels_scope_validity_and_forged_ids(store, channel):
    own, foreign, old = seed()
    statements = []
    event.listen(store, "before_cursor_execute", lambda c, cur, sql, args, ctx, many: statements.append((sql, args)))
    operations = {
        "vector": lambda: facts.knn(VECTOR, 20, user_id="B"),
        "keyword": lambda: facts.keyword_search("Python", 20, user_id="B"),
        "structured": lambda: facts.structured_search(20, user_id="B"),
    }
    with user_scope("A"):
        assert [f["id"] for f in operations[channel]()] == [own["id"]]
    reads = [(sql, args) for sql, args in statements if "FROM memory_facts" in sql]
    assert reads
    assert all("memory_facts.user_id = ?" in sql and "memory_facts.valid_until IS NULL" in sql
               and "A" in args for sql, args in reads)
    with user_scope("B"):
        assert [f["id"] for f in retrieval.retrieve("Python")] == [foreign["id"]]


def test_tool_scoped_pending_read_only_and_kind_filter(store):
    own, _, _ = seed()
    with user_scope("A"):
        facts.add_fact("Python target city", kind="city", confirmed=True)
        before = facts.structured_search()
        result = tool_registry.invoke_tool("memory_facts_search", {
            "query": "Python", "kind": "preference", "limit": 10, "user_id": "B", "owner_id": "B",
        }, {"user_id": "B"})
        assert result == [{"id": own["id"], "text": own["text"], "kind": "preference", "confirmed": False}]
        assert facts.structured_search() == before
        for limit in (0, 11):
            error = tool_registry.invoke_tool("memory_facts_search", {"query": "Python", "limit": limit})
            assert error["error"]["code"] == "schema_validation"
        assert tool_registry.get("memory_facts_search").risk_level == "read"
        assert "user_id" not in tool_registry.get("memory_facts_search").params_schema["properties"]


def test_model_is_offered_memory_tool_only_when_enabled(store, monkeypatch, uid):
    from app.agent import tool_selection
    prompts = []
    monkeypatch.setattr(tool_selection.llm_gateway, "has_llm", lambda *a: True)
    def choose(system, user, **kwargs):
        prompts.append(system)
        return {"tool": "memory_facts_search", "confidence": 1.0}
    monkeypatch.setattr(tool_selection.llm_gateway, "chat_json", choose)
    selected = tool_selection.select_tool("之前说过什么偏好")
    assert selected.tool == "memory_facts_search" and selected.intent == "chat"
    assert '"name": "memory_facts_search"' in prompts[-1]
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "0")
    assert tool_selection.select_tool("之前说过什么偏好") is None
    assert '"name": "memory_facts_search"' not in prompts[-1]


def test_missing_identity_cannot_be_supplied_in_arguments_or_metadata(store):
    for scope in (None, system_scope()):
        from contextlib import nullcontext
        with scope or nullcontext():
            result = tool_registry.invoke_tool("memory_facts_search", {"query": "Python", "user_id": "A"}, {"user_id": "A"})
            assert result["error"]["code"] == "identity_required"
            for operation in (lambda: facts.keyword_search("Python"), facts.structured_search,
                              lambda: retrieval.retrieve("Python")):
                with pytest.raises(IdentityRequired):
                    operation()


def test_pending_context_profile_and_trusted_owner(store):
    seed()
    with user_scope("A"):
        profile.save("A", {"name": "Applicant A", "interests": ["Python"]})
        text = context_assembler.memory_context("B", query="Python")
        assert "Applicant A" in text and "Python A pending" in text
        assert "待确认（非用户确认，不可作为事实陈述）" in text
        assert "不是用户确认的事实" in text
        assert "B private" not in text and "A invalid" not in text
        assembled = context_assembler.assemble_context({"user_id": "A"}, query="Python")
        assert "Python A pending" in assembled and "待确认" in assembled


@pytest.mark.parametrize("flag", [None, "0", "false"])
def test_flag_off_context_byte_identical_and_no_tool_or_calls(store, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("MEMORY_VECTOR_ENABLED")
    else:
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", flag)
    def unexpected(*args, **kwargs):
        pytest.fail("vector memory touched while disabled")
    for obj, name in ((embeddings, "embed_text"), (reranking, "rerank"),
                      (facts, "knn"), (facts, "keyword_search"), (facts, "structured_search")):
        monkeypatch.setattr(obj, name, unexpected)
    with user_scope("A"):
        profile.save("A", {"name": "Applicant", "interests": ["Python"]})
        episodic.write_event("A", "chat", "Python backend")
        assert episodic.search_events("B", query="Python")[0]["summary"] == "Python backend"
        expected = profile.format_for_prompt("A") + "\n\n【相关记忆】\n- [chat] Python backend"
        assert context_assembler.memory_context("A", query="Python") == expected
        actual = context_assembler.assemble_context({"user_id": "A"}, query="Python")
        # The old assembler wrapping and allocation, independent of the new branch.
        from app.tools.prompt_data import data_block
        assert actual == data_block("memory", expected)
        assert retrieval.retrieve("Python") == []
        assert tool_registry.get("memory_search").fn is episodic.search_events.__wrapped__
        assert tool_registry.invoke_tool("memory_search", {"query": "Python"})[0]["summary"] == "Python backend"
        assert tool_registry.get("memory_facts_search") is None
        assert "memory_facts_search" not in {t["name"] for t in tool_registry.list_tools()}
        assert tool_registry.invoke_tool("memory_facts_search", {"query": "Python"})["error"]["code"] == "unknown_tool"


def test_rrf_merges_channels_deduplicates_and_breaks_ties():
    a, b, c = ({"id": i} for i in (1, 2, 3))
    assert retrieval.rrf([[a, b], [b, c], [c, b]]) == [b, c, a]
    assert retrieval.rrf([[b], [a]]) == [a, b]
    assert retrieval.rrf([[a, a], [b]]) == [a, b]


def fail(*args, **kwargs):
    raise RuntimeError("offline")


def test_embed_failure_keeps_keyword_and_structured(store, monkeypatch):
    with user_scope("A"):
        keyword = facts.add_fact("Python historical event", kind="event")
        structured = facts.add_fact("Shanghai goal", kind="goal")
        monkeypatch.setattr(embeddings, "embed_text", fail)
        result = retrieval.retrieve("Python")
        assert {row["id"] for row in result} == {keyword["id"], structured["id"]}


@pytest.mark.parametrize("bad_order", [None, [0, 0], [99], [True]])
def test_rerank_failure_or_invalid_indices_keeps_rrf(store, monkeypatch, bad_order):
    with user_scope("A"):
        for text in ("Python first", "Python second"):
            facts.add_fact(text, kind="preference")
        expected = retrieval.rrf([facts.knn(VECTOR, 20), facts.keyword_search("Python"), facts.structured_search()])
        monkeypatch.setattr(reranking, "rerank", fail if bad_order is None else lambda *a, **kw: bad_order)
        assert [row["id"] for row in retrieval.retrieve("Python")] == [row["id"] for row in expected]


def test_reranker_reorders_only_scoped_candidates(store, monkeypatch):
    seed()
    observed = []
    def rank(query, documents, **kwargs):
        observed.extend(documents)
        return list(reversed(range(len(documents))))
    monkeypatch.setattr(reranking, "rerank", rank)
    with user_scope("A"):
        second = facts.add_fact("Python second", kind="preference", confirmed=True)
        expected = retrieval.rrf([facts.knn(VECTOR, 20), facts.keyword_search("Python"), facts.structured_search()])
        result = retrieval.retrieve("Python")
        assert [f["id"] for f in result] == [f["id"] for f in reversed(expected)]
        assert any(f["id"] == second["id"] and f["confirmed"] for f in result)
    assert set(observed) == {"Python A pending", "Python second"}


@pytest.mark.parametrize("budget", [0, 1, 30, 100, 300, 600])
def test_token_cap_includes_labels_and_markup(store, budget):
    with user_scope("A"):
        for i in range(12):
            facts.add_fact(f"Python <& 中文 {i} " + "长内容" * 20, kind="preference", confirmed=i % 2 == 0)
        selected = retrieval.retrieve("Python", token_cap=budget)
        assert len(selected) <= 6
        assert estimate_tokens(retrieval.render_facts(selected)) <= budget
        text = context_assembler.assemble_context({"user_id": "A"}, query="Python",
                                                policy=context_assembler.ContextPolicy(token_budget=budget))
        assert estimate_tokens(text) <= budget
        if text:
            assert text.startswith("<memory>\n") and text.endswith("\n</memory>")


def test_top20_channels_and_tool_limit(store, monkeypatch):
    with user_scope("A"):
        for i in range(25):
            facts.add_fact(f"Python {i}", kind="preference", confirmed=True)
        assert len(facts.knn(VECTOR, 20)) == len(facts.keyword_search("Python")) == len(facts.structured_search()) == 20
        assert len(retrieval.retrieve("Python", limit=8, token_cap=2000)) == 8
        assert len(tool_registry.invoke_tool("memory_facts_search", {"query": "Python", "limit": 10})) <= 10


def test_large_profile_cannot_crowd_out_pending_facts(store):
    with user_scope("A"):
        profile.save("A", {"name": "Applicant", "facts": ["Long profile fact " * 100] * 20})
        facts.add_fact("Python pending evidence", kind="preference")
        text = context_assembler.assemble_context({"user_id": "A"}, query="Python")
        assert "Python pending evidence" in text and "待确认" in text
        assert estimate_tokens(text) <= context_assembler.ContextPolicy().token_budget


@pytest.mark.parametrize("budget", [600, 1200, 2400])
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("padding", ["长期偏好" * 70, "<&>" * 70])
def test_all_sections_keep_only_complete_facts(store, budget, confirmed, padding):
    from html import escape, unescape

    with user_scope("A"):
        profile.save("A", {"name": "Applicant", "facts": ["个人备注 " * 50] * 4})
        state = {"user_id": "A", "thread_id": "whole-facts",
                 "draft": {"kind": "email", "title": "请假邮件", "body": "草稿正文 " * 200}}
        saved = context_assembler.conversation_state(state)
        saved.update(summary="此前摘要 " * 200,
                     turns=[{"user": "最近提问 " * 200, "assistant": "最近回答 " * 200}])
        state["conversation_context"] = saved
        long_fact = facts.add_fact("Python " + padding + "，但我不喜欢这个安排。",
                                   kind="preference", confirmed=confirmed)
        short_fact = facts.add_fact("Python 不接受夜班。", kind="preference", confirmed=confirmed)
        assert estimate_tokens(retrieval.render_facts([long_fact])) < retrieval.FACT_TOKEN_CAP
        policy = context_assembler.ContextPolicy(token_budget=budget)
        text = context_assembler.assemble_context(state, query="Python", policy=policy)
        for heading in ("近期对话", "当前草稿", "此前对话摘要", "个人档案"):
            assert heading in text
        assert estimate_tokens(text) <= budget
        assert long_fact["text"] not in unescape(text)
        # Every emitted fact must be a complete JSON entry, including negation.
        lines = [line for line in unescape(text).splitlines() if line.startswith("- [")]
        for line in lines:
            payload = json.loads(line.split("] ", 1)[1])
            assert payload == {"kind": "preference", "text": short_fact["text"]}
            assert line == retrieval.render_facts([short_fact]).splitlines()[-1]
        if budget == 2400:
            assert len(lines) == 1
            assert escape(retrieval.render_facts([short_fact]), quote=False) in text
        if not lines:
            assert "相关事实记忆" not in text


def test_chat_receives_facts_before_generation(store, monkeypatch, fake_llm):
    from app.agent.subgraphs import chat
    monkeypatch.setattr(chat, "maybe_summarize_turn", lambda *a, **kw: None)
    monkeypatch.setattr(chat, "_llm_risk", lambda message: False)
    with user_scope("A"):
        facts.add_fact("Python pending preference", kind="preference")
        chat.node_chat({"user_id": "A", "thread_id": "retrieval-chat", "message": "Python 怎么学习"})
        assert "Python pending preference" in fake_llm.calls[-1][0]
        assert "待确认（非用户确认，不可作为事实陈述）" in fake_llm.calls[-1][0]


def test_query_and_candidates_redacted_before_providers(store, monkeypatch):
    seen_queries, seen_docs = [], []
    def embed(text, **kwargs):
        seen_queries.append(text)
        return embeddings.Embedding(VECTOR, embeddings.DEFAULT_MODEL)
    def rank(query, documents, **kwargs):
        seen_queries.append(query)
        seen_docs.extend(documents)
        return list(range(len(documents)))
    with user_scope("A"):
        profile.save("A", {"privacy_sandbox": ["HiddenProject"]})
        facts.add_fact("Python HiddenProject", kind="preference", embedding=VECTOR)
        monkeypatch.setattr(embeddings, "embed_text", embed)
        monkeypatch.setattr(reranking, "rerank", rank)
        result = retrieval.retrieve("Python HiddenProject")
        assert seen_queries and seen_docs
        assert "HiddenProject" not in json.dumps([seen_queries, seen_docs, result])


def test_all_channels_fail_without_failing_context(store, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_text", fail)
    monkeypatch.setattr(facts, "keyword_search", fail)
    monkeypatch.setattr(facts, "structured_search", fail)
    with user_scope("A"):
        profile.save("A", {"name": "Applicant"})
        assert retrieval.retrieve("Python") == []
        assert "Applicant" in context_assembler.assemble_context({"user_id": "A"}, query="Python")


@pytest.mark.parametrize("staged", [False, True])
def test_retrieval_sees_transactional_additions_and_invalidations(store, staged):
    from app.business_stage import stage_business_writes
    transaction = stage_business_writes if staged else db.memory_transaction
    with user_scope("A"):
        old = facts.add_fact("Python old preference", kind="preference", confirmed=True)
        with transaction():
            facts.invalidate_fact(old["id"])
            new = facts.add_fact("Python new preference", kind="preference", confirmed=True)
            assert [row["id"] for row in retrieval.retrieve("Python")] == [new["id"]]
            result = tool_registry.invoke_tool("memory_facts_search", {"query": "Python"})
            assert [row["id"] for row in result] == [new["id"]]


def test_keyword_literal_wildcards_and_structured_kinds(store):
    with user_scope("A"):
        facts.add_fact("abcXdef", kind="event")
        literal = facts.add_fact("abc_def", kind="event")
        assert [f["id"] for f in facts.keyword_search("abc_def")] == [literal["id"]]
        assert facts.structured_search() == []
        assert len(facts.structured_search(kind="event")) == 2


@pytest.mark.parametrize("stage", ["embed", "rerank"])
def test_slow_provider_does_not_block_reply(store, monkeypatch, stage):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        try:
            release.wait(2)
            raise RuntimeError("provider late")
        finally:
            finished.set()
    monkeypatch.setattr(retrieval, "CHANNEL_TIMEOUT", 0.1)
    monkeypatch.setattr(retrieval, "RERANK_TIMEOUT", 0.1)
    with user_scope("A"):
        facts.add_fact("Python keyword fallback", kind="preference")
        monkeypatch.setattr(embeddings if stage == "embed" else reranking,
                            "embed_text" if stage == "embed" else "rerank", slow)
        try:
            started = time.monotonic()
            assert retrieval.retrieve("Python")
            assert time.monotonic() - started < 1.0
            assert entered.is_set()
        finally:
            release.set()
            assert finished.wait(1)


def test_rerank_gateway_contract_and_flag_off(monkeypatch, uid):
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    # Stub model authentication and transport; no network or real credential access.
    spec = get_config().rerank_model()
    monkeypatch.setattr(type(spec), "api_key", lambda self: "synthetic")
    monkeypatch.setattr(reranking, "get_config", lambda: SimpleNamespace(rerank_model=lambda task: spec))
    calls, records = [], []
    def post(*args):
        calls.append(args)
        return {"results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.9}]}
    monkeypatch.setattr(providers, "_post", post)
    monkeypatch.setattr(reranking.usage, "record", lambda **kw: records.append(kw))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "0")
    assert reranking.rerank("Python", ["a", "b"]) == [0, 1]
    assert not calls and not records
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    assert reranking.rerank("Python", ["a", "b"]) == [1, 0]
    assert calls[0][1].endswith("/v1/rerank")
    assert calls[0][3] == {"model": "BAAI/bge-reranker-v2-m3", "query": "Python",
                            "documents": ["a", "b"], "top_n": 2, "return_documents": False}
    assert calls[0][4] <= 2.0
    assert records[0]["task"] == "memory_rerank" and records[0]["ok"]


@pytest.mark.parametrize("payload", [{}, {"results": []}, {"results": [{"index": 99, "relevance_score": 1}]},
                                     {"results": [{"index": 0, "relevance_score": float("nan")}]},
                                     {"results": [{"index": False, "relevance_score": 1}]}])
def test_rerank_gateway_rejects_malformed_response(monkeypatch, payload, uid):
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    spec = get_config().rerank_model()
    monkeypatch.setattr(type(spec), "api_key", lambda self: "synthetic")
    monkeypatch.setattr(providers, "_post", lambda *a: payload)
    with pytest.raises(providers.ProviderError, match="invalid memory rerank response"):
        reranking.rerank("query", ["document"])


def test_rerank_task_configuration(monkeypatch):
    monkeypatch.delenv("LLM_CONFIG_FILE", raising=False)
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({
        "models": {"ranker": {"model": "BAAI/bge-reranker-v2-m3", "base_url": "https://example.invalid/v1"}},
        "rerank_tasks": {"memory_rerank": "ranker"},
    }))
    assert get_config().rerank_model().id == "ranker"
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({"rerank_tasks": {"memory_rerank": "missing"}}))
    with pytest.raises(ConfigError, match="undeclared model"):
        get_config()


def test_synthetic_ab_mock_mode(tmp_path):
    from scripts.eval_memory_retrieval import run_eval
    path = tmp_path / "memory-ab.json"
    report = run_eval(mode="mock", output=path)
    assert json.loads(path.read_text()) == report
    assert report["questions"] >= 36 and report["users"] >= 2
    assert report["facts_per_user"] > report["k"]
    assert report["hybrid_recall_at_k"] >= report["old_recall_at_k"]
    assert report["hybrid_recall_at_k"] > 0
