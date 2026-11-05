"""No model download or external credentials in CI.

The hybrid scorer is a safety net only: it may add a crisis override or ask for
clarification, never dispatch a task from message text. Tasks stay behind the
offline rules (confirm / draft) or model tool selection.
"""
import json
import math
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest

from app.agent import intent_scoring as scoring
from app.agent import router

DRAFT_TEXT = "帮我写一封请假邮件，周五请一天假"
VAGUE_TEXT = "帮我弄一下那个"
EXEMPLAR_COUNT = 40
EMBED_BATCHES = 4  # 40 exemplars / 10 per request


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        pytest.fail("intent tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def hybrid(monkeypatch):
    monkeypatch.setenv("INTENT_SCORING_MODE", "hybrid")
    monkeypatch.setattr(scoring, "second_opinion", lambda *args: False)
    def install(label, confidence):
        scores = {k: 0.1 for k in scoring.LABELS}
        scores[label] = .8
        evidence = dict(confidence=confidence, margin=.1, winner=label,
                        model_version="mock", exemplar_version="test", hosted=False)
        monkeypatch.setattr(scoring, "score_text", lambda _: (scores, evidence))
    return install


def test_labels_are_safety_only():
    assert scoring.LABELS == ("confirm", "chat", "unresolved", "crisis")
    assert router._HYBRID_MESSAGE_INTENTS == set()


@pytest.mark.parametrize("confidence,override", [(.95, True), (.85, True), (.55, True), (.549, False), (0., False)])
def test_crisis_threshold_boundaries(hybrid, confidence, override):
    hybrid("crisis", confidence)
    result = router.classify_text_result(VAGUE_TEXT)
    assert result.safety_override is override
    assert result.intent == "chat"
    if override:
        assert result.rule_id == "crisis" and result.confidence is None
    else:
        assert result.decision == "fallback" and result.confidence == confidence
        assert result.score_source == "embedding_knn"
    assert router.route_intent(text=VAGUE_TEXT) == "chat"


def test_semantic_chat_is_a_fallback_not_a_route(hybrid):
    hybrid("chat", .95)
    result = router.classify_text_result("你好")
    assert result.decision == "fallback" and not result.needs_clarify
    assert result.score_source == "embedding_knn"
    assert router.route_intent(text="你好") == "chat"


@pytest.mark.parametrize("mode", ["rules", "shadow", "hybrid", "invalid"])
@pytest.mark.parametrize("state", [{}, {"action": "confirm"}, {"_resume": {}}, {"action": "draft"}])
def test_crisis_bypasses_all_scoring(monkeypatch, mode, state):
    monkeypatch.setenv("INTENT_SCORING_MODE", mode)
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("crisis must never score"))
    state = {**state, "message": "不想活了，帮我写封邮件"}
    expected = "chat" if mode == "hybrid" else state.get("action", "confirm" if "_resume" in state else "chat")
    assert router.route_intent(state) == expected
    result = router.intent_result_for(state, state["message"])
    if mode == "hybrid":
        assert result.safety_override and result.confidence is None
        assert result.rule_id == "crisis" and result.probabilities == {}
    else:
        assert asdict(result) == dict(intent="chat", confidence=1.0, candidates=["chat"],
                                     rule_id="crisis", needs_clarify=False, decision="route")


def test_embedding_adds_crisis_and_state_precedes_cache(hybrid):
    hybrid("crisis", .7)
    assert router.classify_text_result("今晚就是最后一晚").safety_override
    hybrid("chat", .99)
    state = {}
    assert not router.intent_result_for(state, VAGUE_TEXT).safety_override
    state["crisis_detected"] = True
    assert router.intent_result_for(state, VAGUE_TEXT).safety_override


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "1.1", "bad", ".2"])
def test_invalid_thresholds_fail_closed(hybrid, monkeypatch, value):
    hybrid("crisis", .99)
    monkeypatch.setenv("INTENT_SCORE_ROUTE_THRESHOLD", value)
    result = router.classify_text_result(VAGUE_TEXT)
    assert asdict(result) == asdict(router._classify_rules_result(VAGUE_TEXT))
    assert result.confidence == .2 and result.decision == "fallback"
    assert not result.safety_override


def test_shadow_never_scores_and_state_cache(monkeypatch, hybrid):
    hybrid("unresolved", .7)
    state = {}
    result = router.intent_result_for(state, VAGUE_TEXT)
    assert result.decision == "clarify"
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("must not score"))
    assert router.intent_result_for(state, VAGUE_TEXT) == result
    monkeypatch.setenv("INTENT_SCORING_MODE", "shadow")
    assert router.intent_result_for(state, VAGUE_TEXT).score_source == "rule_prior"


def test_old_checkpoint():
    old = dict(intent="chat", confidence=.2, candidates=["chat"], rule_id="chat_fallback", needs_clarify=False, decision="fallback")
    assert router.IntentResult.from_dict(old).score_source == "rule_prior"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1, 2])
def test_invalid_policy(bad):
    with pytest.raises(ValueError):
        scoring.unit_interval(bad)


@pytest.fixture
def embedding_http(monkeypatch, tmp_path):
    import httpx
    calls = []
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-only")
    monkeypatch.setattr(scoring, "VECTOR_CACHE", tmp_path / "vectors.json")
    def post(url, *, headers, json, timeout):
        assert url == "https://api.siliconflow.cn/v1/embeddings"
        assert headers == {"Authorization": "Bearer test-only"}
        assert json["model"] == "Pro/BAAI/bge-m3"
        assert json["encoding_format"] == "float"
        assert timeout == 30
        calls.append(json["input"])
        # Exactly one crisis exemplar contains 「自杀」, so that query scores 1/3 with k=3.
        data = [{"index": i, "embedding": [1., 0.] if "自杀" in text else [0., 1.]}
                for i, text in enumerate(json["input"])]
        return httpx.Response(200, json={"data": data[::-1]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    scoring._load.cache_clear()
    yield calls
    scoring._load.cache_clear()


def test_real_knn_math_and_cached_startup(monkeypatch, embedding_http):
    monkeypatch.setenv("INTENT_SCORING_MODE", "hybrid")
    assert scoring.build_vector_cache() == (EXEMPLAR_COUNT, 2)
    assert len(embedding_http) == EMBED_BATCHES
    scoring.warmup()
    scoring.warmup()
    assert len(embedding_http) == EMBED_BATCHES  # cache loading makes no HTTP calls
    scores, evidence = scoring.score_text("自杀")
    assert len(embedding_http) == EMBED_BATCHES + 1
    assert scores["crisis"] == pytest.approx(1/3)
    assert evidence["margin"] == pytest.approx(1/3)
    assert evidence["confidence"] == pytest.approx(2/3)
    assert evidence["winner"] == "crisis"
    assert evidence["hosted"] is True
    assert evidence["model_version"] == scoring.EMBEDDING_MODEL


def test_vector_cache_matches_exemplars():
    # The cache is generated locally (scripts/embed_intents.py) and not shipped.
    if not scoring.VECTOR_CACHE.exists():
        pytest.skip("exemplar vector cache not generated")
    cached = json.loads(scoring.VECTOR_CACHE.read_text())
    assert len(cached["vectors"]) == EXEMPLAR_COUNT
    assert all(cached[k] == v for k, v in scoring.cache_metadata().items())


def test_missing_cache_fails_closed(embedding_http):
    assert not scoring.VECTOR_CACHE.exists()
    with pytest.raises(RuntimeError, match="initialization failed"):
        scoring.score_text("hello")
    assert len(embedding_http) == 0  # never rebuilt implicitly


@pytest.mark.parametrize("field,value", [("model", "other-model"), ("exemplars_sha256", "stale"),
                                         ("vectors", [[1., 0.]]), ("vectors", [[float("nan")]])])
def test_invalid_cache_fails_closed(embedding_http, field, value):
    scoring.build_vector_cache()
    cached = json.loads(scoring.VECTOR_CACHE.read_text())
    cached[field] = value
    scoring.VECTOR_CACHE.write_text(json.dumps(cached))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="initialization failed"):
            scoring.score_text("hello")
    assert len(embedding_http) == EMBED_BATCHES


def test_missing_key_never_uses_other_credentials(monkeypatch, embedding_http):
    scoring.build_vector_cache()
    monkeypatch.delenv("SILICONFLOW_API_KEY")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "wrong-provider")
    with pytest.raises(KeyError):
        scoring.score_text("hello")
    assert len(embedding_http) == EMBED_BATCHES


@pytest.mark.parametrize("payload", [[], [{"index": 1, "embedding": [1, 0]}],
                                     [{"index": 0, "embedding": [0, 0]}],
                                     [{"index": 0, "embedding": [float("inf"), 1]}]])
def test_invalid_http_vectors(monkeypatch, embedding_http, payload):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(
        200, content=json.dumps({"data": payload}), request=httpx.Request("POST", a[0])))
    with pytest.raises((RuntimeError, ValueError)):
        scoring.HostedEncoder().encode(["hello"])


def test_http_error_is_sanitized(monkeypatch, embedding_http):
    import httpx
    def fail(*a, **kw):
        raise RuntimeError("test-only secret provider body")
    monkeypatch.setattr(httpx, "post", fail)
    with pytest.raises(RuntimeError) as exc:
        scoring.HostedEncoder().encode(["hello"])
    assert str(exc.value) == "SiliconFlow embedding request failed"
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("label", ["confirm", "draft", "update_profile", "schedule"])
@pytest.mark.parametrize("confidence", [.1, .7, .99])
def test_hybrid_never_dispatches_tasks_from_scores(hybrid, label, confidence):
    hybrid(label, confidence)
    text = "帮我处理一下这件事"
    assert asdict(router.classify_text_result(text)) == asdict(router._classify_rules_result(text))
    assert router.route_intent(text=text) == "chat"


def test_hybrid_clarification_offers_only_chat(hybrid):
    hybrid("unresolved", .1)
    result = router.classify_text_result(VAGUE_TEXT)
    assert result.needs_clarify
    assert result.candidates == ["chat"]


def test_unresolved_option_card(client, uid, hybrid):
    hybrid("unresolved", .6)
    response = client.post("/api/chat", json={"user_id": uid, "message": VAGUE_TEXT})
    assert response.status_code == 200
    data = response.json()["data"]
    from app.agent.ask_cards import INTENT_LABELS, INTENT_QUESTION
    assert data["ask"]["question"] == INTENT_QUESTION
    assert data["ask"]["type"] == "single"
    assert data["ask"]["options"][-1] == INTENT_LABELS["chat"]
    assert set(data["ask"]["options"][:-1]) <= {INTENT_LABELS["draft"], INTENT_LABELS["update_profile"]}
    assert len(data["ask"]["options"]) >= 2


def test_model_failure_does_not_execute(hybrid, monkeypatch):
    monkeypatch.setattr(scoring, "score_text", lambda _: 1/0)
    result = router.classify_text_result(VAGUE_TEXT)
    assert asdict(result) == asdict(router._classify_rules_result(VAGUE_TEXT))
    assert result.confidence == .2
    assert result.intent == "chat" and result.decision == "fallback"


def test_crisis_at_end_of_long_message(monkeypatch):
    monkeypatch.setenv("INTENT_SCORING_MODE", "hybrid")
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("no scoring"))
    state = {"action": "draft", "message": "会议纪要 " * 5000 + "不想活了"}
    assert router.route_intent(state) == "chat"
    assert state["crisis_detected"] is True


def test_retained_fields_are_not_current_turn_inputs(monkeypatch):
    monkeypatch.setenv("INTENT_SCORING_MODE", "hybrid")
    state = {"message": "你好", "draft_request": "不想活了", "intent_input_fields": ["message"]}
    assert router.current_turn_inputs(state) == {"message": "你好"}


def test_eval_scaffold_no_exemplar_leakage():
    data = scoring.exemplars()
    rows = [json.loads(line) for line in scoring.EXEMPLARS.with_name('eval.jsonl').read_text().splitlines()]
    texts = {text for group in data['intents'].values() for text in group}
    assert sum(len(group) for group in data['intents'].values()) == EXEMPLAR_COUNT
    assert len(rows) >= 40
    assert {row['label'] for row in rows} <= set(scoring.LABELS)
    assert sum(row['confusable'] for row in rows) >= 10
    assert not texts.intersection(row['text'] for row in rows)
    assert len({row['text'] for row in rows}) == len(rows)


@pytest.mark.parametrize("token,lp,expected", [('A', -.01, ('confirm', math.exp(-.01))), ('A', float('nan'), None), ('Z', -.01, None)])
def test_gateway_logprobs(monkeypatch, token, lp, expected):
    from app.gateway import llm_gateway as gateway, core
    def complete(*args, **kwargs):
        assert kwargs['logprobs'] and kwargs['max_tokens'] == 1 and kwargs['tier'] == 'fast'
        return core.Result(content=token, logprobs={'content': [{'token': token, 'logprob': lp}]})
    monkeypatch.setattr(core, 'complete', complete)
    assert gateway.classify_label('你好', scoring.LABELS) == expected


def test_gateway_without_logprobs(monkeypatch):
    from app.gateway import llm_gateway as gateway, core
    monkeypatch.setattr(core, 'complete', lambda *a, **kw: core.Result(content='A'))
    assert gateway.classify_label('hello', scoring.LABELS) is None


def test_provider_logprob_wire_and_unsupported(monkeypatch):
    from app.gateway import providers
    # Minimal spec keeps the test independent of configured vendor credentials.
    spec = SimpleNamespace(model='small', vendor='test', base_url='https://invalid.test', max_tokens=100, params={})
    def post(spec, url, headers, body, timeout):
        assert body['logprobs'] is True and body['top_logprobs'] == 20
        assert body['max_tokens'] == 1 and body['temperature'] == 0
        return {'choices': [{'message': {'content': 'A'}, 'logprobs': {'content': [{'token': 'A', 'logprob': -.01}]}}]}
    monkeypatch.setattr(providers, '_post', post)
    kwargs = dict(api_key='test-only', system='s', user='u', tier='fast', timeout=1, temperature=0, max_tokens=1, logprobs=True)
    assert providers.call_openai(spec, **kwargs).logprobs['content'][0]['token'] == 'A'
    with pytest.raises(providers.ProviderError, match='unsupported'):
        providers.call_anthropic(spec, **kwargs)


@pytest.mark.parametrize("text,intent", [(DRAFT_TEXT, "draft"), ("确认", "confirm"), ("取消", "confirm")])
@pytest.mark.parametrize("label", ["chat", "unresolved", "crisis"])
def test_confident_rules_win_over_model(hybrid, monkeypatch, text, intent, label):
    hybrid(label, .99)
    legacy = router._classify_rules_result(text)
    assert legacy.rule_id == f"heuristic_{intent}"
    scores, evidence = scoring.score_text(text)
    assert router._decide_scored_intent(scores, evidence, legacy, text) == legacy
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("confident rule must not score"))
    assert router.classify_text_result(text) == legacy
    assert router.route_intent(text=text) == intent


def test_default_rules_do_not_load_or_score(monkeypatch):
    monkeypatch.delenv("INTENT_SCORING_MODE", raising=False)
    monkeypatch.setattr(scoring, "_resource", lambda: pytest.fail("default must not load model"))
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("default must not score"))
    scoring.warmup()
    assert router.classify_text_result(DRAFT_TEXT).intent == "draft"
    assert router.classify_text_result(VAGUE_TEXT).score_source == "rule_prior"


@pytest.mark.parametrize("mode", [None, "rules", "shadow", "invalid"])
@pytest.mark.parametrize("text,intent,confidence,candidates,rule,decision", [
    ("你好", "chat", .2, ["chat"], "chat_fallback", "fallback"),
    ("不想活了", "chat", 1., ["chat"], "crisis", "route"),
    (DRAFT_TEXT, "draft", .9, ["draft", "chat"], "heuristic_draft", "route"),
    ("确认", "confirm", .9, ["confirm", "chat"], "heuristic_confirm", "route"),
    ("取消", "confirm", .9, ["confirm", "chat"], "heuristic_confirm", "route"),
    (VAGUE_TEXT, "chat", .2, ["chat"], "chat_fallback", "fallback"),
])
def test_rules_serialization_and_cache_match_main(monkeypatch, caplog, mode, text, intent, confidence, candidates, rule, decision):
    if mode is None:
        monkeypatch.delenv("INTENT_SCORING_MODE", raising=False)
    else:
        monkeypatch.setenv("INTENT_SCORING_MODE", mode)
    expected = dict(intent=intent, confidence=confidence, candidates=candidates, rule_id=rule,
                    needs_clarify=decision == "clarify", decision=decision)
    state = {"user_id": "unchanged", "crisis_detected": True}
    with caplog.at_level("INFO", logger="app.agent.router"):
        first = router.intent_result_for(state, text)
        assert router.intent_result_for(state, text) == first
    assert json.dumps(asdict(first)) == json.dumps(expected)
    assert state == {"user_id": "unchanged", "crisis_detected": True,
                     "intent_message": text, "intent_result": expected}
    assert [r.intent_result for r in caplog.records if hasattr(r, "intent_result")] == [expected]


@pytest.mark.parametrize("mode", [None, "rules", "hybrid"])
def test_explicit_routes_do_not_classify(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("INTENT_SCORING_MODE", raising=False)
    else:
        monkeypatch.setenv("INTENT_SCORING_MODE", mode)
    monkeypatch.setattr(router, "classify_text_result", lambda *a, **kw: pytest.fail("explicit route must not classify"))
    assert set(router._ACTION_MAP) == {"update_profile", "draft", "confirm", "chat"}
    for action, intent in router._ACTION_MAP.items():
        if action == "chat":
            continue
        state = {"action": action, "message": "hello", "user_id": "owner"}
        before = dict(state)
        assert router.route_intent(state) == intent
        assert state == before
    state = {"_resume": {}, "message": "hello", "user_id": "owner"}
    assert router.route_intent(state) == "confirm"
    assert state == {"_resume": {}, "message": "hello", "user_id": "owner"}


@pytest.mark.parametrize("intent,tool", [("draft", "draft_text"), ("chat", "web_search"), ("chat", None)])
def test_hybrid_preserves_main_tool_selection(hybrid, monkeypatch, intent, tool):
    from app.agent import tool_selection
    monkeypatch.setattr(tool_selection, "select_tool", lambda _: tool_selection.Selection(tool, intent, .95))
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("model selection must not be overridden"))
    assert router.route_intent(text="帮我处理一下这件事") == intent


@pytest.mark.parametrize("threshold,decision", [(".95", "clarify"), (".99", "fallback")])
def test_hybrid_preserves_rule_thresholds(hybrid, monkeypatch, threshold, decision):
    monkeypatch.setenv("INTENT_ROUTE_THRESHOLD", ".99")
    monkeypatch.setenv("INTENT_FALLBACK_THRESHOLD", ".95" if decision == "fallback" else ".3")
    monkeypatch.setattr(scoring, "score_text", lambda _: pytest.fail("rule hit must not score"))
    result = router.classify_text_result(DRAFT_TEXT)
    assert result.decision == decision
    assert router.route_intent(text=DRAFT_TEXT) == "chat"


def test_low_confidence_compatibility_api_cannot_execute(hybrid):
    hybrid("unresolved", .1)
    result = router.classify_text_result(VAGUE_TEXT)
    assert result.decision == "clarify" and result.needs_clarify
    assert result.candidates == ["chat"]
    assert router.classify_text(VAGUE_TEXT) == "chat"


@pytest.mark.parametrize("confidence", [0., .549, .55, .85, 1.])
def test_unresolved_always_clarifies(hybrid, confidence):
    hybrid("unresolved", confidence)
    result = router.classify_text_result("不知道下一步做什么")
    assert result.intent == "chat"
    assert result.needs_clarify and result.decision == "clarify"
    assert router.route_intent(text="不知道下一步做什么") == "chat"


@pytest.mark.parametrize("failure", ["missing_key", "empty_key", "timeout", "api_error"])
def test_embedding_failure_returns_exact_rules(monkeypatch, embedding_http, failure, caplog):
    import httpx
    scoring.build_vector_cache()
    monkeypatch.setenv("INTENT_SCORING_MODE", "hybrid")
    if failure == "missing_key":
        monkeypatch.delenv("SILICONFLOW_API_KEY")
    elif failure == "empty_key":
        monkeypatch.setenv("SILICONFLOW_API_KEY", " ")
    else:
        def fail(*args, **kwargs):
            if failure == "timeout":
                raise httpx.TimeoutException("private provider detail")
            return httpx.Response(503, text="private provider detail", request=httpx.Request("POST", args[0]))
        monkeypatch.setattr(httpx, "post", fail)
    expected = router._classify_rules_result(VAGUE_TEXT)
    assert asdict(router.classify_text_result(VAGUE_TEXT)) == asdict(expected)
    assert router.route_intent(text=VAGUE_TEXT) == "chat"
    assert len(embedding_http) == EMBED_BATCHES
    assert "private provider detail" not in caplog.text


def test_user_embeddings_are_not_cached(monkeypatch, embedding_http):
    from app.data_access import user_scope
    scoring.build_vector_cache()
    original = scoring.VECTOR_CACHE.read_bytes()
    for uid in ("intent-owner-a", "intent-owner-b", "intent-owner-a"):
        with user_scope(uid):
            scoring.score_text("same user query")
    assert embedding_http[EMBED_BATCHES:] == [["same user query"]] * 3
    assert scoring.VECTOR_CACHE.read_bytes() == original
