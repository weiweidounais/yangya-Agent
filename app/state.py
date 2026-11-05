"""LangGraph shared state for the Personal Agent flow."""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict


class ProfileDict(TypedDict, total=False):
    """Generic personal profile. Every field is optional and user-editable."""

    name: str
    city: str
    tz: str
    occupation: str
    interests: list[str]
    preferences: list[str]
    facts: list[str]
    privacy_sandbox: list[str]


class DraftDict(TypedDict, total=False):
    kind: str  # email | message | article | note | other
    title: str
    body: str
    notes: list[str]


class AgentState(TypedDict, total=False):
    """Full graph state. Fields are optional so nodes can accumulate."""

    user_id: str
    thread_id: str
    action: Literal["update_profile", "draft", "confirm", "chat"]
    message: str
    intent_result: Optional[dict[str, Any]]
    intent_message: str
    intent_input_fields: list[str]
    profile: ProfileDict
    draft_request: str
    draft: DraftDict
    confirmed: bool
    confirm_payload: dict[str, Any]
    messages: list[dict[str, str]]
    conversation_context: dict[str, Any]
    pending_schedule: Optional[dict[str, Any]]
    failed_step: Optional[str]
    error: Optional[str]
    # --- Agent loop (budgets + counters; checkpointed) ---
    loop_step: int
    loop_redraft_count: int
    loop_retries: int
    loop_started_at: float
    loop_last_error: Optional[str]
    loop_tokens: int
    loop_cost_units: float
    loop_feedback: list[dict[str, str]]
    loop_budget: dict[str, Any]
    loop_stop_reason: Optional[str]
    loop_tier: str
    loop_escalated: bool
    loop_escalation: Optional[dict[str, Any]]
    loop_history: list[dict[str, Any]]
    loop_wrap_up: bool
    loop_continues: int
    loop_continued: bool
    loop_task_payload: dict[str, Any]
    pending_continue: Optional[dict[str, Any]]
    next_suggestion: Optional[str]
    pending_redraft: bool
    # --- Structured question cards (chat) ---
    ask: Optional[dict[str, Any]]
    reply_to: Optional[dict[str, Any]]
    crisis_detected: bool
    channel: str
