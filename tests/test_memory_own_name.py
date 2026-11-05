"""B15: recall preserves the active user's name and memory rows are plain text."""

import pytest

from app import db
from app.agent.harness import leak
from app.agent.harness.guardrails import guard_node_output
from app.agent.memory import episodic
from app.agent.memory.card import card_view
from app.agent.memory.redact import display_text
from app.agent.subgraphs import chat
from app.data_access import user_scope
from app.gateway import llm_gateway


NAME = "测试用户甲"
REPLY = f"我记得你是**{NAME}**，做**后端工程师**。"


@pytest.fixture
def other_profiles(uid):
    # A matching display name is not proof that the output belongs to someone
    # else. An unrelated third party must still be hidden in the same reply.
    with user_scope(uid + "-namesake"):
        db.save_profile(uid + "-namesake", {"name": NAME})
    with user_scope(uid + "-other"):
        db.save_profile(uid + "-other", {"name": "测试用户乙"})


@pytest.mark.parametrize("persisted", [True, False])
@pytest.mark.parametrize("question", ["你记得我什么", "记忆"])
def test_recall_keeps_own_name_in_reply_storage_and_drawer(
    uid, monkeypatch, other_profiles, persisted, question,
):
    profile = {"name": NAME, "experience": ["后端工程师"]}
    if persisted:
        db.save_profile(uid, profile)
    monkeypatch.setattr(llm_gateway, "chat_text", lambda *a, **kw: REPLY)
    monkeypatch.setattr(chat, "_llm_risk", lambda msg: False)
    monkeypatch.setattr(chat, "maybe_summarize_turn", lambda *a, **kw: None)

    result = chat.node_chat({"user_id": uid, "profile": profile, "message": question})
    # Exercise both pre-storage chat guards and the final node output guard.
    result, hits = guard_node_output(result, user_id=uid, inputs=(question,))
    assert result["messages"][-1]["content"] == REPLY
    assert not hits
    with db.SessionLocal() as session:
        rows = session.query(episodic.EpisodicEvent).filter_by(user_id=uid, kind="chat").all()
        assert len(rows) == 1
        assert NAME in rows[0].summary
        assert leak.OTHER_USER_PLACEHOLDER not in rows[0].summary
    episodes = card_view(uid)["episodes"]
    assert len(episodes) == 1
    assert episodes[0]["value"] == f"我记得你是{NAME}，做后端工程师。"


def test_shared_index_ownership_survives_partial_profile(uid, other_profiles):
    db.save_profile(uid, {"name": NAME})
    result = leak.scan_output(
        f"{NAME}，测试用户乙", user_id=uid, profile={}, strict=True,
    )
    assert result["text"] == f"{NAME}，{leak.OTHER_USER_PLACEHOLDER}"
    assert result["hits"] == [{"type": "other_user_data", "count": 1}]


def test_own_name_sandbox_still_takes_precedence(uid, other_profiles):
    db.save_profile(uid, {"name": NAME, "privacy_sandbox": [NAME]})
    result = leak.scan_output(REPLY, user_id=uid, strict=True)
    assert NAME not in result["text"]
    assert leak.SANDBOX_PLACEHOLDER in result["text"]


def test_final_guard_saved_name_edit_wins(uid, other_profiles):
    db.save_profile(uid, {"name": "测试用户丙"})
    result, hits = guard_node_output(
        {"profile": {"name": NAME},
         "messages": [{"role": "assistant", "content": f"测试用户丙，{NAME}"}]},
        user_id=uid,
    )
    assert result["messages"][0]["content"] == f"测试用户丙，{leak.OTHER_USER_PLACEHOLDER}"
    assert hits[0]["type"] == "other_user_data"


def test_legacy_drawer_rows_remove_markdown(uid):
    # Existing redaction cannot be reversed safely, but its emphasis must not
    # appear literally in the drawer (including rows predating this fix).
    with db.SessionLocal.begin() as session:
        session.add(episodic.EpisodicEvent(
            user_id=uid, kind="chat",
            summary="我记得你是**[他人信息已隐藏]**，做**后端工程师**。",
        ))
    assert card_view(uid)["episodes"][0]["value"] == (
        "我记得你是[他人信息已隐藏]，做后端工程师。"
    )


@pytest.mark.parametrize("text,expected", [
    ("**后端工程师**", "后端工程师"),
    ("**后端工程师", "后端工程师"),
    ("***后端工程师***", "后端工程师"),
    ("*后端工程师*", "后端工程师"),
    ("__后端工程师__", "后端工程师"),
])
def test_memory_emphasis_is_plain_text(text, expected):
    assert display_text(text) == expected
