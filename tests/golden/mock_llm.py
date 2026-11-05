"""Scripted mock LLM for golden cases (installed through the gateway override seam).

Default behaviour is a *well-behaved* model: tools get their deterministic
fallbacks, chat gets a short empathetic reply, the crisis classifier follows
the lexicon. Cases script *misbehaving* outputs (leaks, invented hotlines) to
prove the guardrails catch them regardless of what the model says.

Drafts never reach the model in mock mode: ``draft_text`` falls back to
``app.tools.writing.offline_draft`` when no key is configured.
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.memory.summary import SUMMARY_SYSTEM
from app.agent.subgraphs.chat import CHAT_SYSTEM, CRISIS_NOTE, PERSONA, _RISK_SYS, has_crisis_signal
from app.tools.writing import DRAFT_SYSTEM

DEFAULT_CHAT = "听起来你最近挺不容易的。愿意多说一点吗？我在这儿听着。"
DEFAULT_CRISIS_CHAT = "谢谢你愿意告诉我，我很在乎你现在的感受。我在这儿陪着你。"


class ScriptedLLM:
    def __init__(self, script: Optional[dict[str, Any]] = None):
        self.script = dict(script or {})
        self.calls: list[dict[str, Any]] = []
        self._chat_queue = self._as_list(self.script.get("chat"))

    @staticmethod
    def _as_list(v: Any) -> list[Any]:
        if v is None:
            return []
        return list(v) if isinstance(v, list) else [v]

    def seen_text(self) -> str:
        return "\n".join(c["system"] + "\n" + c["user"] for c in self.calls)

    def __call__(self, kind: str, system: str, user: str, tier: Any, fallback: Any) -> Any:
        role = self._role(system)
        self.calls.append({"kind": kind, "role": role, "system": system, "user": user, "tier": str(tier)})
        if role == "risk":
            if "risk" in self.script:
                return str(self.script["risk"])
            return "YES" if has_crisis_signal(user) else "NO"
        if role == "chat":
            if self._chat_queue:
                return str(self._chat_queue.pop(0))
            return DEFAULT_CRISIS_CHAT if CRISIS_NOTE in system else DEFAULT_CHAT
        if role == "draft" and "draft" in self.script:
            draft = self.script["draft"]
            if isinstance(draft, dict):
                return dict(draft)
            return {"kind": "other", "title": "草稿", "body": str(draft)}
        if role == "summary":
            return self.script.get("summary", "")
        return fallback

    @staticmethod
    def _role(system: str) -> str:
        if system.startswith(_RISK_SYS):
            return "risk"
        if system.startswith(CHAT_SYSTEM) or system.startswith(PERSONA):
            return "chat"
        if system.startswith(DRAFT_SYSTEM):
            return "draft"
        if system.startswith(SUMMARY_SYSTEM):
            return "summary"
        return "other"
