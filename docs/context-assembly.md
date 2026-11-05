# Retrieval and conversation context

`app.agent.context_assembler` assembles prompt memory on demand. The existing
`format_memory_context` facade now uses it, so analyze/draft callers also get
bounded keyword retrieval without changes to their tool registrations.

Chat checkpoints store `conversation_context`, scoped to user, thread and memory
revision. A turn is a completed user/assistant exchange. Defaults retain four
turns, each side capped at 600 estimated tokens. Only evicted turns and the prior
summary go through the central gateway (`task="summary"`, JSON, 400 output tokens,
10-second timeout). No keys, empty output or gateway failure uses a bounded
extractive fallback. Crisis turns skip this additional LLM call. The existing
background profile-summary pipeline remains independent.

The assembled chat memory block is capped at 2,400 estimated tokens, including
escaped XML boundaries. Recent turns appear newest first, followed by the rolling
summary and retrieved profile/events. Under pressure, lower-priority context is
clipped first. This budget covers the memory block; persona, current request and
answer-card context are separate. It is an estimate using the existing CJK-aware
meter, not a vendor tokenizer. `ContextPolicy` parameters configure these limits;
zero raw turns compresses every completed exchange, zero context budget emits no
memory, and negative limits are rejected.

Episodic lookup runs only with a nonempty query or explicit tags. It matches
Latin keywords and Chinese bigrams, ranks by keyword overlap and breaks ties by
recency. SQL filters by user and matching terms before taking up to 500 candidates,
so unrelated recent activity does not hide older matches. Results are limited to
six in chat. Empty queries still allow profile context but do not dump recent
events. Existing explicit `search_events` behavior is unchanged.

Memory edits increment `memory_rev`, invalidating checkpoint conversation context
on the next use. Credentials and privacy-sandbox values are masked before prompts
and persistence; retrieved material, raw turns and summaries remain untrusted data.

Offline regression coverage: `tests/unit/test_context_assembler.py` uses the gateway
override and exercises rolling input, fallback, isolation, privacy, retrieval,
chat integration and exact rendered estimated-budget limits.
