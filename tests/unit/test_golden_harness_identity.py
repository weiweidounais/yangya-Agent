"""Judge context reads must carry the golden case's identity (no HTTP needed)."""
import json
from types import SimpleNamespace

import pytest

from app import db
from app.data_access import IdentityRequired, current_user, user_scope
from tests.golden.run_golden import Runner


def test_judge_reads_case_owner_and_restores_identity(monkeypatch):
    from app.evals import judge as judge_module

    reads = []

    def profile(user_id):
        reads.append((current_user(), user_id))
        return {"name": "case-owner"}

    monkeypatch.setattr(db, "get_profile", profile)
    monkeypatch.setattr(judge_module, "judge", lambda rubric, **kwargs: kwargs)
    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(judge_mode="heuristic")
    state = {"user_id": "case-owner", "last_output": "听起来很难受，想聊聊吗？", "last_input": "方案又被打回了",
             "last_op": "chat", "context": "fallback"}
    with user_scope("outer-owner"):
        result = runner._judge({"category": "comfort", "judge": {"rubric": "empathy"}}, state)
        assert current_user() == "outer-owner"
    assert reads == [("case-owner", "case-owner")]
    assert json.loads(result[0]["context"]) == {"name": "case-owner"}
    with pytest.raises(IdentityRequired):
        current_user()


def test_draft_cards_are_not_judged_as_chat(monkeypatch):
    from app.evals import judge as judge_module

    monkeypatch.setattr(db, "get_profile", lambda user_id: pytest.fail("draft output must not trigger a profile read"))
    monkeypatch.setattr(judge_module, "judge", lambda rubric, **kwargs: pytest.fail("no rubric judges drafts"))
    runner = Runner.__new__(Runner)
    runner.args = SimpleNamespace(judge_mode="heuristic")
    state = {"user_id": "case-owner", "last_output": "草稿正文", "last_input": "帮我写一封请假邮件",
             "last_op": "draft", "context": ""}
    with user_scope("case-owner"):
        assert runner._judge({"category": "comfort"}, state) == []
