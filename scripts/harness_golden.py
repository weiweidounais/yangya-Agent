#!/usr/bin/env python3
"""Golden regression for Agent Loop + Harness (CI gate).

Offline by default (mock LLM, drafts from ``offline_draft``). Optional live:
  HARNESS_LIVE=1 .venv/bin/python scripts/harness_golden.py

Covers: profile → draft → reject → auto redraft → confirm → next-step message
Plus: traces API shape, privacy gate, group-chat guard, foreign-thread confirm.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmpdir = tempfile.mkdtemp(prefix="personal_agent_harness_")
os.environ["DATA_DIR"] = tmpdir
os.environ["PROACTIVE_ENABLED"] = "0"
os.environ["AGENT_MAX_REDRAFTS"] = os.environ.get("AGENT_MAX_REDRAFTS") or "1"
os.environ["AGENT_MAX_STEPS"] = os.environ.get("AGENT_MAX_STEPS") or "30"
os.environ.setdefault("AUTH_SECRET", "harness-golden-" + os.urandom(16).hex())

LIVE = (os.getenv("HARNESS_LIVE") or "").strip() in ("1", "true", "True", "yes")
_LLM_KEYS = (
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "LLM_FAST_API_KEY",
    "LLM_STRONG_API_KEY",
)


def _force_mock() -> None:
    if LIVE:
        return
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"  # a filled .env must not re-add the keys below
    for _k in _LLM_KEYS:
        os.environ.pop(_k, None)


_force_mock()

from app import confirm_store, db
from app.data_access import user_scope
from app.agent.harness.guardrails import group_chat_blocks_personal_data
from app.agent.harness.traces import list_traces
from app.agent.loop import LoopBudget, can_redraft, loop_snapshot
from app.gateway.llm_gateway import gateway_info, has_llm
from app.graph import run_action
from app.main import ConfirmThreadError, _apply_confirm, _handle_channel_inbound
from app.channels import bind_channel, parse_openclaw_webhook
from app.privacy import redact_sensitive
from app.tools.writing import guess_kind

# Imports may load_dotenv — force mock again unless HARNESS_LIVE
_force_mock()

PROFILE = {
    "name": "张三",
    "city": "上海",
    "occupation": "后端工程师",
    "interests": ["跑步", "摄影"],
    "preferences": ["早上九点前不要打扰"],
    "facts": ["下周三去杭州出差", "负责公司内部 Agent 编排服务"],
}

DRAFT_REQUEST = "帮我写一封请假邮件，周五请一天假去看医生，发给我的主管王经理"


def _ok(name: str) -> None:
    print(f"[PASS] {name}")


def _text(messages) -> str:
    return " ".join(str(m.get("content") or "") for m in (messages or []))


@user_scope("harness")
def main() -> int:
    if not LIVE:
        assert not has_llm(), "offline harness must use mock LLM"
    db.init_db()
    db.Base.metadata.create_all(db.ENGINE)

    info = gateway_info()
    assert info.get("loop", {}).get("budgets") is True, info
    assert info.get("harness", {}).get("traces") is True, info
    assert "langgraph" in str(info.get("orchestration") or "")
    _ok("gateway loop+harness flags")

    budget = LoopBudget.from_env()
    assert budget.max_redrafts >= 1
    _ok(f"budget max_redrafts={budget.max_redrafts}")

    print("== profile ==")
    out_p = run_action({"user_id": "harness", "action": "update_profile", "profile": PROFILE})
    saved = out_p["state"].get("profile") or {}
    assert saved.get("name") == "张三" and saved.get("facts"), out_p
    assert db.get_profile("harness").get("city") == "上海"
    _ok("profile")

    print("== draft → interrupt ==")
    tid = "harness-gold-" + os.urandom(3).hex()
    out1 = run_action(
        {"user_id": "harness", "action": "draft", "message": DRAFT_REQUEST,
         "draft_request": DRAFT_REQUEST, "thread_id": tid, "channel": "web"},
        thread_id=tid,
    )
    assert out1["awaiting_confirm"], out1
    draft1 = out1["state"].get("draft") or {}
    body1 = draft1.get("body") or ""
    assert body1 and draft1.get("kind") == guess_kind(DRAFT_REQUEST) == "email", draft1
    if not LIVE:
        assert "【离线草稿】" in body1 and "张三" in body1, body1
    interrupt = out1.get("interrupt") or {}
    assert interrupt.get("type") == "confirm_draft" and interrupt.get("body") == body1, interrupt
    snap = loop_snapshot(out1["state"])
    assert snap["step"] >= 1, snap
    _ok(f"draft interrupt kind={draft1.get('kind')} loop_step={snap['step']}")

    print("== reject → auto redraft → interrupt again ==")
    assert can_redraft(out1["state"]) or int(out1["state"].get("loop_redraft_count") or 0) < budget.max_redrafts
    rej = _apply_confirm(user_id="harness", thread_id=tid, confirmed=False, channel="web", reason="语气太生硬")
    assert rej["awaiting_confirm"] is True, rej
    assert rej["status"] == "awaiting_confirm", rej
    data = rej.get("data") or {}
    assert (data.get("loop") or {}).get("redraft_count", 0) >= 1, data
    draft2 = data.get("draft") or {}
    assert draft2.get("body"), draft2
    assert "语气太生硬" in _text(data.get("messages")), data.get("messages")
    _ok(f"redraft count={(data.get('loop') or {}).get('redraft_count')}")

    print("== confirm → next-step message ==")
    conf = _apply_confirm(user_id="harness", thread_id=tid, confirmed=True, channel="web")
    assert conf["status"] == "confirmed" and conf["awaiting_confirm"] is False, conf
    cdata = conf.get("data") or {}
    suggestion = str(cdata.get("next_suggestion") or "") or _text(cdata.get("messages"))
    assert "已确认" in suggestion, suggestion
    assert (cdata.get("draft") or {}).get("body"), cdata
    decision = confirm_store.get_decision(tid)
    assert decision and decision.get("confirmed") is True and decision.get("user_id") == "harness", decision
    _ok("confirmed with next-step message")

    print("== traces ==")
    traces = list_traces(tid, limit=50)
    assert traces, traces
    tools = {t.get("tool") for t in traces}
    assert {"draft", "await_confirm"} & tools, tools
    assert any(t.get("ok") for t in traces)
    for t in traces:
        for key in ("input_summary", "result_summary", "latency_ms", "guardrails", "llm_calls"):
            assert key in t, (key, t)
    _ok(f"traces n={len(traces)} tools={sorted(t for t in tools if t)}")

    print("== privacy gate ==")
    redacted, hits = redact_sensitive("我的手机号是 13811112222，邮箱密码是 Harness#Pass2026")
    assert "Harness#Pass2026" not in redacted and "13811112222" not in redacted, redacted
    assert {"password", "phone"} <= set(hits), hits
    assert redact_sensitive("明天早上九点提醒我交周报")[1] == []
    _ok(f"privacy gate hits={sorted(set(hits))}")

    print("== guardrail: group chat ==")
    assert group_chat_blocks_personal_data(True) is True
    assert group_chat_blocks_personal_data(False) is False
    bind_channel("openclaw_wechat", "wx_harness", "harness")
    wxg = parse_openclaw_webhook(
        {
            "event": "message",
            "from": "wx_harness",
            "text": "帮我看看我档案里记了什么 " + ("x" * 50),
            "room_id": "room-h",
            "msg_id": "h1",
        }
    )
    assert wxg["inbound"]["is_group"] is True
    blocked_g = _handle_channel_inbound(wxg, "openclaw_wechat")
    assert blocked_g["status"] == "group_blocked", blocked_g
    out_txt = str((blocked_g.get("outbound") or {}).get("text") or "")
    assert "私聊" in out_txt or "个人信息" in out_txt, out_txt
    assert "杭州出差" not in out_txt and "张三" not in out_txt
    _ok("group blocked")

    print("== foreign / unknown thread confirm ==")
    bogus = "no-such-thread-" + os.urandom(4).hex()
    try:
        _apply_confirm(user_id="harness", thread_id=bogus, confirmed=True)
        raise AssertionError("expected ConfirmThreadError")
    except ConfirmThreadError as e:
        assert e.http_status == 404
    assert confirm_store.get_decision(bogus) is None
    with user_scope("harness-other"):
        try:
            _apply_confirm(user_id="harness-other", thread_id=tid, confirmed=False)
            raise AssertionError("expected ConfirmThreadError for a foreign thread")
        except ConfirmThreadError as e:
            assert e.http_status == 404
    assert confirm_store.get_decision(tid).get("confirmed") is True
    _ok("unknown + foreign thread confirm refused")

    mode = "live" if LIVE and has_llm() else "offline"
    print(
        json.dumps(
            {
                "result": "HARNESS_GOLDEN_PASS",
                "mode": mode,
                "tmpdir": tmpdir,
                "thread_id": tid,
                "trace_count": len(traces),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print("HARNESS_GOLDEN_FAIL:", e, file=sys.stderr)
        raise
