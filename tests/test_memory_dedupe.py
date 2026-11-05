"""H7 regressions using synthetic turns and the offline summary fallback."""

import pytest

from app.data_access import user_scope

from app.agent.memory import episodic, profile
from app.agent.memory.summary import maybe_summarize_turn, flush_summaries, reset_summary_scheduler


@pytest.fixture(autouse=True)
def offline_summary(monkeypatch):
    from app.gateway import llm_gateway
    monkeypatch.setattr(llm_gateway, "has_llm", lambda *args, **kwargs: False)
    reset_summary_scheduler()
    yield
    assert flush_summaries(5)
    reset_summary_scheduler()


def turn(uid, text="希望远程工作", reply="这是合成助手的完整答复。", tid="session"):
    maybe_summarize_turn(uid, kind="chat", user_text=text, assistant_text=reply, thread_id=tid)
    assert flush_summaries(5)


def test_completed_turns_upsert_summary(client, uid):
    turn(uid, text="希望在上海工作")
    first_id = episodic.search_events(uid, kind="summary")[0]["id"]
    turn(uid, text="现在只考虑杭州")
    turn(uid, text="最新偏好是远程工作")
    events = episodic.search_events(uid, kind="summary")
    assert len(events) == 1
    assert events[0]["id"] == first_id
    assert "最新偏好是远程工作" in events[0]["summary"]
    assert "上海" not in events[0]["summary"]
    assert "杭州" not in events[0]["summary"]
    response = client.get(f"/api/memory/{uid}", params={"kind": "summary"})
    assert response.status_code == 200
    assert len(response.json()["data"]["events"]) == 1


def test_fallback_preserves_assistant_reply(uid):
    reply = "这是合成助手的完整答复。"
    turn(uid, reply=reply)
    assert reply in profile.get(uid)["dialogue_summary"]
    assert reply in str(episodic.search_events(uid))


def test_credentials_never_enter_summary(uid):
    secret = "hunter2synthetic"
    text = f"网盘密码：{secret} 另外希望远程工作"
    turn(uid, text=text, reply=f"已记下。密码：{secret} 不会外传。")
    summary = profile.get(uid).get("dialogue_summary", "")
    assert "远程工作" in summary
    assert secret not in summary
    assert secret not in str(episodic.search_events(uid))


def test_normal_generated_summary_preserved(uid):
    from app.agent.memory.summary import get_scheduler

    facts = "兴趣：跑步、摄影。习惯：早起。计划：周末去爬山。希望远程工作。"
    get_scheduler().set_generator(lambda job: facts)
    turn(uid, text="希望远程工作")
    assert profile.get(uid)["dialogue_summary"] == facts
    assert episodic.search_events(uid, kind="summary")[0]["summary"] == facts


def test_normal_chat_memory_and_repeated_summaries(client, uid, fake_llm):
    fake_llm.reply = "收到。"
    message = "Remote work is required."
    thread_id = None
    first_id = None
    for _ in range(3):
        payload = {"user_id": uid, "message": message}
        if thread_id:
            payload["thread_id"] = thread_id
        response = client.post("/api/chat", json=payload)
        assert response.status_code == 200
        data = response.json()["data"]
        thread_id = response.json()["thread_id"]
        assert flush_summaries(5)
        assert message in profile.get(uid)["dialogue_summary"]
        events = client.get(f"/api/memory/{uid}", params={"kind": "summary"})
        assert events.status_code == 200
        summaries = events.json()["data"]["events"]
        assert len(summaries) == 1
        assert message in summaries[0]["summary"]
        if first_id is None:
            first_id = summaries[0]["id"]
        assert summaries[0]["id"] == first_id


@pytest.mark.parametrize("message", [
    "兴趣：跑步、摄影。习惯：早起。计划：周末去爬山。希望远程工作。",
    "我住在杭州。\n平时喜欢做饭。\n养了一只猫。\n希望远程工作。",
])
def test_ordinary_personal_facts_at_chat_boundary(uid, fake_llm, message):
    from app.agent.subgraphs import chat

    fake_llm.reply = "收到。"
    chat.node_chat({"user_id": uid, "message": message, "thread_id": "facts"})
    assert flush_summaries(5)
    assert message.replace("\n", " ") in profile.get(uid)["dialogue_summary"]


def test_user_and_session_isolation(uid):
    from app.agent.memory.summary import SummaryJob

    other = uid + "-other"
    assert SummaryJob(uid, "chat", "", "", "same").key() != SummaryJob(other, "chat", "", "", "same").key()
    turn(uid, text="第一会话", tid="same")
    with user_scope(other):
        turn(other, text="另一用户", tid="same")
    turn(uid, text="第二会话", tid="second")
    turn(uid, text="更新第一会话", tid="same")
    mine = {ev["thread_id"]: ev["summary"] for ev in episodic.search_events(uid, kind="summary")}
    assert len(mine) == 2
    assert "更新第一会话" in mine["same"]
    assert "第二会话" in mine["second"]
    with user_scope(other):
        theirs = episodic.search_events(other, kind="summary")
    assert len(theirs) == 1
    assert "另一用户" in theirs[0]["summary"]


def test_existing_duplicate_summaries_collapse(uid):
    from app.db import memory_session

    with memory_session() as session:
        for text in ("旧摘要一", "旧摘要二", "旧摘要三"):
            session.add(episodic.EpisodicEvent(user_id=uid, kind="summary", summary=text, thread_id="session"))
    assert len(episodic.search_events(uid, kind="summary")) == 3
    turn(uid, text="最新对话要点")
    events = episodic.search_events(uid, kind="summary")
    assert len(events) == 1
    assert "最新对话要点" in events[0]["summary"]
    assert "旧摘要" not in events[0]["summary"]


def test_chat_event_preserves_ordinary_reply(uid, fake_llm, monkeypatch):
    from app.agent.subgraphs import chat

    reply = "我们先梳理你希望的作息安排，再一起看看周末计划。" * 20
    fake_llm.reply = reply
    monkeypatch.setattr(chat, "_llm_risk", lambda msg: False)
    result = chat.node_chat({"user_id": uid, "message": "希望远程工作", "thread_id": "chat"})
    assert result["messages"][-1]["content"] == reply
    assert flush_summaries(5)
    events = episodic.search_events(uid, kind="chat")
    assert len(events) == 1
    assert events[0]["summary"] == reply[:240]
    assert reply[:120] in profile.get(uid)["dialogue_summary"]


def test_summary_preserves_sanitized_metadata(uid):
    import json

    secret = "hunter2synthetic"
    maybe_summarize_turn(uid, kind="chat", user_text="希望远程工作", assistant_text="收到。",
                         thread_id="metadata",
                         extra={"source": "synthetic", "note": f"网盘密码：{secret} 已记录", "password": secret})
    assert flush_summaries(5)
    payload = episodic.search_events(uid, kind="summary")[0]["payload"]
    assert payload["source"] == "synthetic"
    assert payload["note"] == "网盘 已记录"
    assert "password" not in payload
    assert secret not in json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _trusted_test_setup(uid):
    from app.data_access import user_scope
    with user_scope(uid):
        yield
