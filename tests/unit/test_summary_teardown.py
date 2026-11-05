"""Test teardown finishes summary persistence and joins its background worker."""
import threading

import pytest

from app.agent.memory import profile, summary


def test_summary_teardown_drains_inflight_work(uid, monkeypatch, _drain_background_summaries):
    started, release = threading.Event(), threading.Event()
    rows = []

    def generate(**kwargs):
        started.set()
        assert release.wait(5.0)
        return "Synthetic summary"

    monkeypatch.setattr(summary, "_generate_summary", generate)
    monkeypatch.setattr("app.agent.tool_registry._sample_read", lambda: True)
    monkeypatch.setattr("app.agent.harness.traces.record_step", lambda **row: rows.append(row))
    try:
        result = summary.maybe_summarize_turn(
            uid, kind="chat", user_text="Synthetic turn", assistant_text="Synthetic reply")
        assert result["scheduled"]
        assert started.wait(5.0)
        worker = summary.get_scheduler()._thread
        assert worker.is_alive()
        rows.clear()  # Only observe audits emitted as the background job finishes.
    finally:
        release.set()
        _drain_background_summaries()

    assert not worker.is_alive()
    assert any(row["tool"] == "profile_read" and row["user_id"] == uid
               and row["thread_id"] == "tool-registry" for row in rows)
    assert profile.get(uid)["dialogue_summary"] == "Synthetic summary"


def test_teardown_waits_for_inflight_trace_redaction(uid, monkeypatch, _drain_background_summaries):
    from app.agent.harness import traces
    from app.data_access import current_user
    from app import db

    assert traces.flush()
    started, release = threading.Event(), threading.Event()
    draining, drained = threading.Event(), threading.Event()
    reads, errors = [], []
    build = traces._build_row
    read = db.read_profile_data

    def held_build(record):
        started.set()
        assert release.wait(5), "test did not release trace writer"
        return build(record)

    def checked_read(user_id):
        reads.append((threading.current_thread().name, current_user(), user_id))
        return read(user_id)

    def drain():
        draining.set()
        try:
            _drain_background_summaries()
        except BaseException as exc:
            errors.append(exc)
        finally:
            drained.set()

    monkeypatch.setattr(traces, "_build_row", held_build)
    monkeypatch.setattr(db, "read_profile_data", checked_read)
    assert traces.record_step(thread_id="teardown-trace", user_id=uid,
                              tool="summary-audit", sync=False) == {"queued": True}
    worker = threading.Thread(target=drain, name="test-drain")
    try:
        assert started.wait(5)
        worker.start()
        assert draining.wait(5)
        assert not drained.wait(.1), "teardown returned while trace redaction was still in flight"
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(5)
        assert traces.flush()

    assert not worker.is_alive()
    assert errors == []
    assert traces.writer_stats()["pending"] == 0
    assert reads == [("trace-writer", uid, uid)]


def test_summary_teardown_resets_generator_before_next_turn(uid, _drain_background_summaries):
    scheduler = summary.get_scheduler()
    scheduler.set_generator(lambda job: "Previous test's generator")
    summary.maybe_summarize_turn(
        uid, kind="chat", user_text="Previous turn", assistant_text="OK")
    worker = scheduler._thread

    _drain_background_summaries()

    assert not worker.is_alive()
    assert profile.get(uid)["dialogue_summary"] == "Previous test's generator"
    summary.maybe_summarize_turn(
        uid, kind="chat", user_text="Next isolated turn", assistant_text="OK")
    assert summary.flush_summaries()
    assert "Next isolated turn" in profile.get(uid)["dialogue_summary"]


@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_timeout_keeps_inflight_worker_owned(uid, monkeypatch, fails):
    scheduler = summary.SummaryScheduler()
    started, release = threading.Event(), threading.Event()

    def generate(job):
        started.set()
        assert release.wait(5)
        if fails:
            raise RuntimeError("Failed during shutdown")
        return "Finished before replacement"

    scheduler.set_generator(generate)
    scheduler.submit(summary.SummaryJob(uid, "chat", "Turn", "Reply"))
    assert started.wait(5)
    worker = scheduler._thread
    join = worker.join
    # Simulate an expired join deadline without sleeping or depending on load.
    monkeypatch.setattr(worker, "join", lambda timeout=None: join(timeout=0))
    try:
        with pytest.raises(TimeoutError, match="summary worker"):
            scheduler.shutdown()
        assert scheduler._thread is worker
        assert worker.is_alive()
    finally:
        release.set()
        join(timeout=5)
        try:
            assert not worker.is_alive()
            assert scheduler.flush(timeout=0), "stopped worker must not enqueue a retry"
        finally:
            scheduler.shutdown()
    assert not worker.is_alive()


def test_teardown_drains_queued_trace_before_restoring_patches(monkeypatch, _drain_background_summaries):
    from app.agent.harness import traces

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    insert, flush = traces._insert, traces.flush
    assert flush()

    def blocked_insert(records):
        started.set()
        assert release.wait(5)
        result = insert(records)
        finished.set()
        return result

    def release_and_flush(*args, **kwargs):
        release.set()
        return flush(*args, **kwargs)

    monkeypatch.setattr(traces, "_insert", blocked_insert)
    monkeypatch.setattr(traces, "flush", release_and_flush)
    traces.record_step(thread_id="teardown-queued-trace", tool="queued-probe", sync=False)
    try:
        assert started.wait(5)
        assert traces.writer_stats()["pending"] > 0
        _drain_background_summaries()
        assert finished.is_set()
        assert traces.writer_stats()["pending"] == 0
        assert any(row["tool"] == "queued-probe"
                   for row in traces.list_traces("teardown-queued-trace"))
    finally:
        release.set()
        assert flush()
