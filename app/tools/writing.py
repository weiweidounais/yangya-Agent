"""Generic writing tool: turn a request plus the user's profile into a draft.

The draft always goes to the confirm card; this tool never sends anything.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from app.agent.harness.leak import register_system_prompt
from app.agent.tool_registry import tool
from app.prompts import load_prompt
from app.tools.output_schemas import DraftOutput, output_contract
from app.tools.prompt_data import UNTRUSTED_DATA_RULE, data_block

MAX_BODY_CHARS = 6000
logger = logging.getLogger(__name__)

DRAFT_SYSTEM = load_prompt(
    "draft.text", "v1",
    untrusted_data_rule=UNTRUSTED_DATA_RULE,
    output_contract=output_contract(DraftOutput),
)
register_system_prompt("draft.text", DRAFT_SYSTEM)

_KIND_HINTS = (
    ("email", re.compile(r"邮件|email|e-mail|信\b|推荐信|回信|letter", re.I)),
    ("message", re.compile(r"消息|短信|回复|朋友圈|祝福|道歉|感谢|message|reply|post", re.I)),
    ("article", re.compile(r"文章|总结|周报|日报|发言|演讲|致辞|公告|通知|说明|essay|speech|memo", re.I)),
    ("note", re.compile(r"笔记|备忘|提纲|大纲|note|outline", re.I)),
)


def guess_kind(request: str) -> str:
    for kind, pattern in _KIND_HINTS:
        if pattern.search(request or ""):
            return kind
    return "other"


def _profile_facts(profile: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "city", "occupation", "interests", "preferences", "facts")
    return {k: profile[k] for k in keep if profile.get(k)}


def offline_draft(request: str, profile: dict[str, Any], feedback: Optional[list[str]] = None) -> dict[str, Any]:
    """Deterministic draft when no model is configured (mock mode)."""
    kind = guess_kind(request)
    name = str((profile or {}).get("name") or "").strip()
    lines = [f"【离线草稿】关于：{request.strip()[:120]}", ""]
    if kind == "email":
        lines += ["您好：", "", "【待补充：正文要点】", "", "谢谢！", name or "【待补充：署名】"]
    elif kind == "message":
        lines += ["【待补充：开头称呼】，" + request.strip()[:60], "【待补充：补充说明】"]
    else:
        lines += ["一、背景", "【待补充】", "二、要点", "【待补充】", "三、结论", "【待补充】"]
    if feedback:
        lines += ["", "（已参考意见：" + "；".join(str(f)[:60] for f in feedback[-3:]) + "）"]
    lines += ["", "配置模型 API Key 后会生成正式内容。"]
    return {"kind": kind, "title": request.strip()[:40] or "草稿", "body": "\n".join(lines)}


@tool("draft_text", risk_level="read", description="根据用户的写作请求和档案生成一版草稿。 Draft a text (email, message, article) for the confirm card.",
      when_to_use="用户要求写、改、润色或翻译一段文字时使用。 Use when the user asks for a piece of writing.",
      when_not_to_use="不会发送邮件或消息；草稿必须经用户确认。 Never sends anything; drafts need user confirmation.",
      timeout=90)
def draft_text(request: str, profile: dict, memory: str = "", feedback: Optional[list[str]] = None) -> dict:
    from app.gateway.llm_gateway import OutputSchemaError, chat_json, has_llm

    request = str(request or "").strip()
    profile = dict(profile or {})
    if not has_llm():
        return offline_draft(request, profile, feedback)
    user = "\n".join(x for x in (
        data_block("request", request),
        data_block("profile", json.dumps(_profile_facts(profile), ensure_ascii=False)),
        data_block("memory", memory) if memory else "",
        data_block("feedback", "\n".join(str(f) for f in feedback)) if feedback else "",
    ) if x)
    try:
        result = chat_json(DRAFT_SYSTEM, user, task="draft", fallback=None, timeout=60, max_tokens=2000,
                           response_model=DraftOutput)
    except OutputSchemaError as exc:
        # The error text carries no private content; the draft falls back to the offline stub.
        logger.error("draft_text: %s", type(exc).__name__)
        return offline_draft(request, profile, feedback)
    if not isinstance(result, dict) or result.get("_parse") or str(result.get("_mode", "")).startswith("mock"):
        return offline_draft(request, profile, feedback)
    try:
        parsed = DraftOutput.model_validate({k: v for k, v in result.items() if not k.startswith("_")})
    except Exception:
        return offline_draft(request, profile, feedback)
    return {"kind": parsed.kind, "title": parsed.title.strip(), "body": parsed.body.strip()[:MAX_BODY_CHARS]}
