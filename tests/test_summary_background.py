"""Background dialogue summaries: do not block the caller, dedupe per thread, log failures."""

from __future__ import annotations

import logging
import threading
import time
import uuid

import pytest

from app.agent.memory import profile as profile_mem
from app.db import init_db
from app.agent.memory.summary import (
    SUMMARY_CAP,
    flush_summaries,
    get_scheduler,
    maybe_summarize_turn,
    reset_summary_scheduler,
)


@pytest.fixture(autouse=True)
def _fresh_scheduler():
    init_db()
    reset_summary_scheduler(retry_delay=0.0, max_attempts=3)
    yield
    reset_summary_scheduler(retry_delay=0.0, max_attempts=1)


def _uid() -> str:
    return "sum_" + uuid.uuid4().hex[:10]


def test_summarize_returns_before_llm_finishes(uid):
    started = threading.Event()

    def gen(job):
        started.set()
        time.sleep(0.6)
        return "只在周末跑步"

    get_scheduler().set_generator(gen)
    t0 = time.perf_counter()
    out = maybe_summarize_turn(
        uid,
        kind="chat",
        user_text="我只在周末跑步",
        assistant_text="记下了",
        thread_id="thread-latency",
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25, elapsed
    assert out["ok"] is True
    assert out["scheduled"] is True
    assert out["summary"] == ""
    assert profile_mem.get(uid).get("dialogue_summary") in (None, "")
    assert started.wait(1.0)
    assert flush_summaries(2.0)
    stored = profile_mem.get(uid).get("dialogue_summary") or ""
    assert "周末" in stored
    assert len(stored) <= SUMMARY_CAP


def test_summary_capped_at_2000_chars(uid):
    def gen(job):
        return "摘要" * 5000

    get_scheduler().set_generator(gen)
    maybe_summarize_turn(
        uid,
        kind="chat",
        user_text="很长",
        assistant_text="很长",
        thread_id="thread-cap",
    )
    assert flush_summaries(2.0)
    stored = profile_mem.get(uid).get("dialogue_summary") or ""
    assert len(stored) <= SUMMARY_CAP
    assert len(stored) >= 2000 - 5


def test_same_thread_does_not_double_summarize(uid):
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def gen(job):
        calls.append(job.user_text)
        if len(calls) == 1:
            started.set()
            assert release.wait(2.0)
        return "记：" + job.user_text

    get_scheduler().set_generator(gen)
    maybe_summarize_turn(
        uid, kind="chat", user_text="turn-1", assistant_text="a", thread_id="same"
    )
    assert started.wait(1.0)
    for i in range(2, 9):
        out = maybe_summarize_turn(
            uid,
            kind="chat",
            user_text=f"turn-{i}",
            assistant_text="a",
            thread_id="same",
        )
        assert out["deduped"] is True
    release.set()
    assert flush_summaries(3.0)
    assert 1 <= len(calls) <= 2
    assert calls[-1] == "turn-8"
    assert "turn-8" in (profile_mem.get(uid).get("dialogue_summary") or "")


def test_different_threads_both_summarize(uid):
    calls: list[str] = []

    def gen(job):
        calls.append(job.thread_id)
        return "ok " + job.thread_id

    get_scheduler().set_generator(gen)
    maybe_summarize_turn(uid, kind="chat", user_text="a", assistant_text="b", thread_id="ta")
    maybe_summarize_turn(uid, kind="chat", user_text="c", assistant_text="d", thread_id="tb")
    assert flush_summaries(2.0)
    assert set(calls) == {"ta", "tb"}


def test_failure_is_logged_as_error_and_retried(caplog, uid):
    reset_summary_scheduler(retry_delay=0.0, max_attempts=2)
    calls = {"n": 0}

    def gen(job):
        calls["n"] += 1
        raise RuntimeError("llm down")

    get_scheduler().set_generator(gen)
    caplog.set_level(logging.DEBUG, logger="app.agent.memory.summary")
    out = maybe_summarize_turn(
        uid,
        kind="chat",
        user_text="失败这一轮",
        assistant_text="没写成",
        thread_id="thread-fail",
    )
    assert out["scheduled"] is True
    assert flush_summaries(2.0)
    assert calls["n"] == 2
    recs = [r for r in caplog.records if r.name == "app.agent.memory.summary"]
    assert recs
    assert all(r.levelno >= logging.ERROR for r in recs)
    assert any("summary failed" in r.getMessage() for r in recs)
    assert any("summary gave up" in r.getMessage() for r in recs)
    assert not any(" ok" in r.getMessage().lower() for r in recs)
    assert not profile_mem.get(uid).get("dialogue_summary")


@pytest.mark.parametrize("operation", ["forget", "edit", "delete", "edit_event", "delete_event", "normal"])
def test_summary_respects_memory_revision(monkeypatch, caplog, operation, uid):
    from app.agent.memory import card, episodic, summary

    profile_mem.save(uid, {"dialogue_summary": "old"})
    event = episodic.write_event(uid, "chat", "old event")
    started = threading.Event()
    release = threading.Event()

    def slow_generate(**kwargs):
        started.set()
        assert release.wait(2.0)
        time.sleep(0.02)
        return "background summary"

    monkeypatch.setattr(summary, "_generate_summary", slow_generate)
    caplog.set_level(logging.DEBUG, logger="app.agent.memory.summary")
    maybe_summarize_turn(uid, kind="chat", user_text="hello", assistant_text="hi")
    try:
        assert started.wait(1.0)
        if operation == "forget":
            card.forget_all(uid, confirm=True)
        elif operation == "edit":
            card.update_fact(uid, "dialogue_summary", "user edited text")
        elif operation == "delete":
            card.delete_fact(uid, "dialogue_summary")
        elif operation == "edit_event":
            card.update_episode(uid, event["id"], "user edited event")
        elif operation == "delete_event":
            card.delete_episode(uid, event["id"])
    finally:
        release.set()
    assert flush_summaries(3.0)
    stored = profile_mem.get(uid)
    expected = {"forget": "", "delete": "", "edit": "user edited text", "normal": "background summary"}
    assert stored.get("dialogue_summary", "") == expected.get(operation, "old")
    events = episodic.search_events(uid, kind="summary")
    assert len(events) == (1 if operation == "normal" else 0)
    assert stored.get("memory_rev", 0) == (0 if operation == "normal" else 1)
    if operation != "normal":
        assert "stale summary dropped" in caplog.messages
    if operation == "forget":
        assert episodic.search_events(uid) == []
        # A later summary uses the retained revision and can write normally.
        maybe_summarize_turn(uid, kind="chat", user_text="new", assistant_text="hi")
        assert flush_summaries(3.0)
        assert profile_mem.get(uid)["dialogue_summary"] == "background summary"
        assert len(episodic.search_events(uid, kind="summary")) == 1


def test_summary_retry_keeps_original_revision(uid):
    from app.agent.memory import card, episodic

    calls = []

    def gen(job):
        calls.append(job.attempt)
        if job.attempt == 1:
            card.forget_all(uid, confirm=True)
            raise RuntimeError("retry")
        return "stale retry"

    get_scheduler().set_generator(gen)
    maybe_summarize_turn(uid, kind="chat", user_text="hello", assistant_text="hi")
    assert flush_summaries(3.0)
    assert calls == [1, 2]
    assert not profile_mem.get(uid).get("dialogue_summary")
    assert episodic.search_events(uid) == []


def test_summary_prompt_attributes_speakers_and_forbids_moral_evaluation(monkeypatch):
    from app.agent.memory import summary
    from app.gateway import llm_gateway

    def chat_text(system, user, **kwargs):
        assert '明确归属说话人' in system
        assert '用户提出' in system and '助手建议' in system and '助手未采纳' in system
        assert '不能把助手的建议或拒绝写成用户的立场' in system
        assert '只记录事实、偏好、决定和待办事项' in system
        assert '禁止道德、诚信、底线等判断或态度评价' in system
        assert '用户：把18%写成25%吧，反正没人查' in user
        assert '助手：建议保留真实数据并优化呈现' in user
        return '用户想把18%改成25%，助手建议保留真实数据并优化呈现。'

    monkeypatch.setattr(llm_gateway, 'has_llm', lambda: True)
    monkeypatch.setattr(llm_gateway, 'chat_text', chat_text)
    assert summary._generate_summary(
        kind='chat', user_text='把18%写成25%吧，反正没人查',
        assistant_text='建议保留真实数据并优化呈现', prior='',
    ) == '用户想把18%改成25%，助手建议保留真实数据并优化呈现。'


def test_summary_filter_removes_r15_evaluation_before_storage(monkeypatch, caplog, uid):
    from app.agent.memory import episodic
    from app.gateway import llm_gateway

    monkeypatch.setattr(llm_gateway, 'has_llm', lambda: True)
    monkeypatch.setattr(llm_gateway, 'chat_text', lambda *args, **kwargs: (
        '用户偏好周末跑步。用户明确拒绝数据造假提议，诚信底线已确认。'
        '助手建议保留真实数据并优化呈现。'
    ))
    caplog.set_level(logging.INFO, logger='app.agent.memory.summary')
    maybe_summarize_turn(uid, kind='chat', user_text='把18%写成25%吧，反正没人查',
                         assistant_text='建议保留真实数据并优化呈现')
    assert flush_summaries(3.0)
    expected = '用户偏好周末跑步。助手建议保留真实数据并优化呈现。'
    assert profile_mem.get(uid)['dialogue_summary'] == expected
    assert episodic.search_events(uid, kind='summary')[0]['summary'] == expected
    assert 'summary filter dropped 1 evaluative sentence(s)' in caplog.messages


@pytest.mark.parametrize('separator', ['。', '！', '？', '\n', '；'])
def test_summary_filter_keeps_neutral_facts(separator):
    from app.agent.memory.summary import _filter_summary

    neutral = '用户想把18%改成25%，助手建议保留真实数据并优化呈现。'
    assert _filter_summary('道德评价' + separator + neutral) == neutral
    assert _filter_summary(neutral) == neutral
    assert _filter_summary('诚信底线已确认。') == ''


def test_fallback_summary_preserves_speaker_attribution():
    from app.agent.memory.summary import _fallback_summary

    assert _fallback_summary(
        kind='chat', user_text='把18%写成25%吧，反正没人查',
        assistant_text='建议保留真实数据并优化呈现',
    ) == '[chat] 用户：把18%写成25%吧，反正没人查；助手：建议保留真实数据并优化呈现'


def test_dialogue_summary_context_marks_uncertainty_and_forbids_moral_restatement(uid):
    from app.agent.context_assembler import memory_context

    profile_mem.save(uid, {'name': '测试用户', 'dialogue_summary': '用户偏好周末跑步。'})
    context = memory_context(uid, limit=0)
    assert '可能有误的机器摘要，不是已核实事实' in context
    assert '不要把助手的建议或拒绝归于用户' in context
    assert '不要向用户复述摘要中的评价' in context
    assert '用户偏好周末跑步。' in context


@pytest.mark.parametrize('term', [
    '诚信底线', '底线已确认', '明确拒绝数据造假', '拒绝造假', '道德评价',
])
def test_summary_filter_preserves_company_integrity_fact(term):
    from app.agent.memory.summary import _filter_summary

    assert _filter_summary(f'公司强调诚信经营。{term}。用户偏好周末跑步。') == (
        '公司强调诚信经营。用户偏好周末跑步。'
    )


@pytest.fixture(autouse=True)
def _trusted_test_setup(uid):
    from app.data_access import user_scope
    with user_scope(uid):
        yield
