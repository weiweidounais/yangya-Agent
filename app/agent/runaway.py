"""Loop-runaway detection — stops a task regardless of remaining budget.

Every agent step (graph node or task-loop attempt) appends an entry to
``loop_history``. A task is stopped when, within the recent window:

  repeat_call   same tool + same args 3 times in a row
  repeat_error  the same error 3 times
  no_progress   5 consecutive steps produced no new data / write / draft
  oscillation   the tail of the last 20 steps cycles A→B→A→B… (period 2–5, 3 cycles)
  risk_signal   captcha / risk-control signal while running an external-action tool

Human decision steps (confirm card) are never recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

WINDOW = 20
REPEAT_CALLS = 3
REPEAT_ERRORS = 3
NO_PROGRESS_STEPS = 5
OSC_CYCLES = 3

# Tools that act outside the user's own workspace (send / submit).
# None is registered by default; see docs/tool-registry.md.
EXTERNAL_TOOLS = frozenset(
    {
        "send_message",
        "send_email",
        "submit_form",
    }
)

RISK_SIGNAL_RE = re.compile(
    r"验证码|captcha|recaptcha|人机验证|安全验证|滑块验证|滑动验证|风控|风险控制|"
    r"异常(访问|操作|登录|行为)|访问(过于)?频繁|操作(过于)?频繁|账号(已)?(被)?(限制|冻结|封)|"
    r"risk[ _-]?control|too many requests|verify you are human|unusual activity",
    re.I,
)

KIND_ZH = {
    "repeat_call": "同一个操作带着同样的参数连续做了 3 次",
    "repeat_error": "同一个错误已经出现了 3 次",
    "no_progress": "连续 5 步都没有新进展",
    "oscillation": "在几个步骤之间来回打转",
    "risk_signal": "目标网站弹出了验证码 / 风控提示",
}


def args_signature(tool: str, args: Any = None) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return f"{tool}:{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


def is_external_task(*names: Optional[str]) -> bool:
    return any(str(n or "").strip().lower() in EXTERNAL_TOOLS for n in names)


def has_risk_signal(*texts: Any) -> bool:
    for t in texts:
        if t is None:
            continue
        s = t if isinstance(t, str) else json.dumps(t, ensure_ascii=False, default=str)
        if RISK_SIGNAL_RE.search(s[:20000]):
            return True
    return False


def make_entry(
    tool: str,
    args: Any = None,
    *,
    error: str = "",
    progress: bool = False,
    risk: bool = False,
    source: str = "node",
) -> dict[str, Any]:
    return {
        "tool": tool,
        "sig": args_signature(tool, args),
        "error": str(error or "")[:200],
        "progress": bool(progress),
        "risk": bool(risk),
        "source": source,
    }


def push(history: Optional[list[dict[str, Any]]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    return (list(history or []) + [entry])[-(WINDOW + 10):]


def _oscillates(sigs: list[str]) -> bool:
    for period in range(2, 6):
        n = period * OSC_CYCLES
        if len(sigs) < n:
            continue
        tail = sigs[-n:]
        block = tail[:period]
        if len(set(block)) >= 2 and all(tail[i] == block[i % period] for i in range(n)):
            return True
    return False


def detect(history: Optional[list[dict[str, Any]]]) -> Optional[tuple[str, dict[str, Any]]]:
    """Return (kind, detail) for the first runaway pattern found, else None."""
    h = list(history or [])[-WINDOW:]
    if not h:
        return None
    last = h[-1]
    if last.get("risk"):
        return "risk_signal", {"tool": last.get("tool")}
    tail = h[-REPEAT_CALLS:]
    if len(tail) == REPEAT_CALLS and len({e.get("sig") for e in tail}) == 1:
        return "repeat_call", {"tool": last.get("tool"), "times": REPEAT_CALLS}
    errs = [e.get("error") for e in h if e.get("error")]
    if last.get("error") and errs.count(last["error"]) >= REPEAT_ERRORS:
        return "repeat_error", {"error": last["error"], "times": errs.count(last["error"])}
    np_tail = h[-NO_PROGRESS_STEPS:]
    if len(np_tail) == NO_PROGRESS_STEPS and not any(e.get("progress") for e in np_tail):
        return "no_progress", {"steps": NO_PROGRESS_STEPS}
    if _oscillates([str(e.get("sig")) for e in h]):
        return "oscillation", {"window": WINDOW}
    return None


def runaway_text(kind: str) -> str:
    what = KIND_ZH.get(kind, "在原地打转")
    if kind == "risk_signal":
        return (
            f"先停一下：{what}。我不会去绕过验证，也不会继续操作，"
            "需要你本人去页面上处理一下；处理好之后告诉我，我们再接着来。"
        )
    return f"先停一下：我发现自己{what}，再做下去只是白白消耗，不如先停下来跟你对一下。"


PROGRESS_KEYS = (
    "profile",
    "draft",
    "next_suggestion",
    "pending_action",
)


def node_progress(state: dict[str, Any], result: dict[str, Any]) -> bool:
    """A node made progress if it produced new data, a write, or a draft."""
    for k in PROGRESS_KEYS:
        if k in result and result[k] and result[k] != state.get(k):
            return True
    return False


NODE_ARG_KEYS = ("action", "message", "draft_request", "loop_feedback")


def node_args(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state.get(k) for k in NODE_ARG_KEYS if state.get(k) not in (None, "", [])}
