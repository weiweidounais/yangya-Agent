"""Draft + human-in-the-loop confirm subgraph (with redraft loop support).

The user asks for a piece of writing (email, message, article…). Drafting runs
through the observe → plan → tool → validate loop: a draft that fails
validation is regenerated with the failure reason (max N retries); a draft the
user rejects is regenerated with the user's reason (max N redrafts). Every
regenerated draft goes back to the confirm card — nothing is sent anywhere.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.agent.loop import add_feedback, bump_redraft, can_redraft, check_budget, progress_report
from app.agent.memory import format_memory_context, write_event
from app.agent.memory.summary import maybe_summarize_turn
from app.agent.subgraphs.profile import load_profile
from app.agent.task_loop import Plan, Validation, run_task_loop
from app.state import AgentState
from app.tools.writing import MAX_BODY_CHARS, draft_text

DEFAULT_REJECT_REASON = "用户拒绝了这一版但没说原因：换一种更贴近用户语气、更简洁的写法"
KIND_LABELS = {"email": "邮件", "message": "消息", "article": "文章", "note": "笔记", "other": "草稿"}


def _uid(state: AgentState) -> str:
    return str(state.get("user_id") or "")


def _tid(state: AgentState) -> str:
    return str(state.get("thread_id") or "")


def _user_reasons(feedback: list[dict[str, str]]) -> list[str]:
    return [f["reason"] for f in feedback if f.get("source") == "user_reject" and f.get("reason")]


def draft_request_text(state: AgentState) -> str:
    return str(state.get("draft_request") or state.get("message") or "").strip()


def node_draft(state: AgentState) -> dict[str, Any]:
    profile = load_profile(state)
    uid = _uid(state)
    request = draft_request_text(state)
    redraft_n = int(state.get("loop_redraft_count") or 0)
    failures: list[Exception] = []
    if not request:
        return {
            "profile": profile,
            "error": "draft:empty_request",
            "messages": [{"role": "assistant", "content": "想让我写什么？说一下用途和大概内容，比如「帮我写一封请假邮件」。"}],
        }

    def observe(ctx) -> dict[str, Any]:
        memory = format_memory_context(uid, query=request[:200], limit=6) if uid else ""
        return {"profile": profile, "memory": memory, "feedback": ctx.reasons()}

    def plan(ctx, obs) -> Plan:
        reasons = obs["feedback"]
        return Plan(
            tool="draft_text",
            args={"memory": obs["memory"], "feedback": reasons},
            note="按反馈重写" if reasons else "首版草稿",
        )

    def act(ctx, p: Plan) -> dict[str, Any]:
        try:
            return draft_text(request, profile, memory=p.args["memory"], feedback=p.args["feedback"] or None)
        except Exception as exc:
            failures.append(exc)
            raise

    def validate(ctx, p: Plan, out: dict[str, Any]) -> Validation:
        body = str((out or {}).get("body") or "").strip()
        if not body:
            return Validation.failed("草稿正文为空")
        if len(body) > MAX_BODY_CHARS:
            return Validation.failed(f"草稿太长（{len(body)} 字），请压到 {MAX_BODY_CHARS} 字以内")
        return Validation.passed()

    result = run_task_loop(
        name="写作草稿",
        state=dict(state),
        observe=observe,
        plan=plan,
        act=act,
        validate=validate,
    )
    if failures and result.output is None:
        raise failures[0]
    loop_patch = result.loop_patch(dict(state))

    if result.status in ("stopped", "runaway"):
        write_event(
            uid,
            "loop_budget_stop" if result.status == "stopped" else "loop_runaway_stop",
            f"出稿停止：{result.stop_reason}",
            tags=["loop", "budget" if result.status == "stopped" else "runaway", "draft"],
            payload={"reason": result.stop_reason, "attempts": result.attempts},
            thread_id=_tid(state),
        )
        return {
            **loop_patch,
            "profile": profile,
            "draft_request": request,
            "error": result.stop_error,
            "messages": [{"role": "assistant", "content": result.message}],
        }

    raw = dict(result.output or {})
    kind = str(raw.get("kind") or "other")
    notes = ["这是草稿，确认前不会发出或保存为最终版本。"]
    user_reasons = _user_reasons(result.feedback)
    if user_reasons:
        notes.append("本版已参考你的意见：" + "；".join(user_reasons[-3:]))
    retried = [t.get("reason") for t in result.trace if t.get("reason")]
    if result.ok and retried:
        notes.append(f"自检发现问题后自动重写了 {len(retried)} 次：" + "；".join(retried[-2:]))
    if result.status in ("exhausted", "wrapped_up"):
        notes.append(
            f"预算快到上限，先交付这一版（自检问题：{retried[-1] if retried else '未知'}），请重点检查"
            if result.status == "wrapped_up"
            else f"自检 {result.attempts} 次仍有问题（{retried[-1] if retried else '未知'}），请重点检查"
        )
    if redraft_n > 0:
        notes.append(f"自动重出稿（第 {redraft_n} 次，拒绝后循环）")
    draft: dict[str, Any] = {
        "kind": kind,
        "title": str(raw.get("title") or "")[:200],
        "body": str(raw.get("body") or "")[:MAX_BODY_CHARS],
        "notes": notes,
        "loop": {"status": result.status, "attempts": result.attempts, "trace": result.trace},
    }
    write_event(
        uid,
        "draft_created",
        f"已生成{KIND_LABELS.get(kind, '草稿')}草稿「{draft['title'] or request[:40]}」，待确认"
        + (f"（重出稿 #{redraft_n}）" if redraft_n else ""),
        tags=["draft", "redraft"] if redraft_n else ["draft"],
        payload={
            "kind": kind,
            "redraft": redraft_n,
            "attempts": result.attempts,
            "loop_status": result.status,
            "user_reasons": user_reasons[-3:],
        },
        thread_id=_tid(state),
    )
    return {
        **loop_patch,
        "profile": profile,
        "draft_request": request,
        "draft": draft,
        "confirmed": False,
        "pending_redraft": False,
        "messages": [
            {
                "role": "assistant",
                "content": (
                    f"按你说的「{user_reasons[-1][:80]}」重新改了一版，再帮我看看合不合适？"
                    if redraft_n and user_reasons
                    else "已重新生成一版草稿，请再次确认。"
                    if redraft_n
                    else f"{KIND_LABELS.get(kind, '草稿')}草稿写好了，请在确认卡上看看；确认前不会发出。"
                ),
            }
        ],
    }


def node_await_confirm(state: AgentState) -> dict[str, Any]:
    """Human-in-the-loop: pause until user confirms the draft."""
    draft = state.get("draft") or {}
    payload = {
        "type": "confirm_draft",
        "title": "请确认这份草稿",
        "kind": draft.get("kind") or "other",
        "draft_title": draft.get("title", ""),
        "body": draft.get("body", ""),
        "notes": draft.get("notes") or [],
        "redraft_count": int(state.get("loop_redraft_count") or 0),
        "reject_reason_supported": True,
    }
    decision = interrupt(payload)
    if not isinstance(decision, dict):
        decision = {"confirmed": bool(decision)}
    confirmed = bool(decision.get("confirmed", False))
    reason = str(decision.get("reason") or "").strip()[:500]
    edits = decision.get("edits") or {}
    new_draft = dict(draft)
    if isinstance(edits, dict):
        if isinstance(edits.get("body"), str) and edits["body"].strip():
            new_draft["body"] = edits["body"][:MAX_BODY_CHARS]
        if isinstance(edits.get("title"), str) and edits["title"].strip():
            new_draft["title"] = edits["title"][:200]

    uid = _uid(state)
    tid = _tid(state)
    loop_patch: dict[str, Any] = {}
    pending_redraft = False
    feedback = list(state.get("loop_feedback") or [])
    if confirmed:
        msg = "已确认。这版就定稿了；要再改、换个语气，或者用在别的地方，随时告诉我。"
    else:
        feedback = add_feedback(
            state, source="user_reject", reason=reason or DEFAULT_REJECT_REASON
        )
        # Check budget BEFORE bump so max_redrafts=1 allows one auto redraft
        if can_redraft(state):
            loop_patch = bump_redraft(state)
            pending_redraft = True
            if reason:
                msg = f"收到，你觉得「{reason[:80]}」。我按这个重新改一版，改好再请你确认。"
            else:
                msg = (
                    f"已拒绝本次草稿；将自动重新出稿"
                    f"（第 {loop_patch.get('loop_redraft_count')} 次），请稍候再确认。"
                )
        else:
            ok, stop = check_budget(state)
            if not ok:
                msg = progress_report(state, stop)
                loop_patch = {"loop_stop_reason": stop}
            else:
                msg = "已拒绝本次草稿，且已达重出稿次数上限；告诉我想怎么改，再说一次需求，我按你的意见重写。"

    write_event(
        uid,
        "confirm_accepted" if confirmed else "confirm_rejected",
        msg,
        tags=["confirm", "accepted" if confirmed else "rejected"],
        payload={
            "confirmed": confirmed,
            "override": bool(decision.get("override")),
            "will_redraft": pending_redraft,
            "redraft_count": loop_patch.get("loop_redraft_count"),
            "reason": reason,
            "kind": new_draft.get("kind"),
        },
        thread_id=tid,
    )
    user_bits = (
        f"用户{'确认' if confirmed else '拒绝'}了{KIND_LABELS.get(str(new_draft.get('kind')), '草稿')}草稿"
        f"「{new_draft.get('title') or draft_request_text(state)[:40]}」。"
        + (f"拒绝原因：{reason}" if reason else "")
    )
    maybe_summarize_turn(
        uid,
        kind="confirm",
        user_text=user_bits,
        assistant_text=msg,
        thread_id=tid,
        extra={"confirmed": confirmed, "redraft": pending_redraft},
    )
    out: dict[str, Any] = {
        "confirmed": confirmed,
        "draft": new_draft,
        "confirm_payload": decision,
        "pending_redraft": pending_redraft,
        "loop_feedback": feedback,
        "messages": [{"role": "assistant", "content": msg}],
    }
    if confirmed:
        out["next_suggestion"] = msg
    out.update(loop_patch)
    return out
