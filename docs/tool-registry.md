# Tool registry

面向产品读者的中文分类清单见 [工具分类清单](tools.md)。

`app/agent/tool_registry.py` enforces the shared execution boundary for decorated
Python entrypoints and `invoke_tool(name, args, ctx)`. Registered tools are exposed at
`/api/tools` and `/api/agent/tools`. Trusted context comes from authenticated server
state, never model-supplied arguments.

`app/agent/tool_catalog.py` contains reviewed Chinese/English descriptions, selection
and exclusion guidance, and budgets for each tool. Contract tests reject short or
placeholder descriptions and recursively empty parameter schemas. Server-only
callbacks, sessions, configuration and datetimes use validated `x-python-type`
extensions; they are not JSON instructions for a model to construct executable objects.

## Risk and confirmation

- `read`: drafting, retrieval and lookups; at most one retry for an invalid return or
  transient connection/transport timeout, within the deadline.
- `self_write`: profile, memory and schedule changes. Proposal creation is a write even
  though it does not apply the proposed change.
- `notify_self`: reminders and deliveries. Arguments must match the authenticated
  owner; delivery verifies the channel binding.
- `external`: anything that acts outside the user's own workspace. None is registered
  by default; adding one requires `requires_confirm` and a one-use, argument-bound
  confirmation token consumed through `confirm_store`.

Every non-read tool has `max_retries=0`, including notifications. Nested external
actions still require their own authorization; a local orchestration call does not
bypass it.

## Execution budgets and uncertain effects

Budgets range from 5 seconds for profile reads to 90 seconds for draft generation.
Availability is checked before input validation or argument repair. Validation and
deadline refusals use plain Chinese messages; schema diagnostics remain in logs.
Nested calls inherit the earliest deadline.

A worker runs each complete call tree with copied authorization/trace context. Nested
calls run on that same worker so a transaction never crosses concurrent workers. If a
side-effecting operation is still in flight at the deadline, returns an invalid result,
or raises an unexpected execution error, the response is
`{ok: null, status: "unknown", operation_id, message}` — not a failure and never
retried automatically. Legacy Python callers receive `ToolStatusUnknown`; the HTTP
boundary returns 202.

## Trace policy

All writes, owner notifications and refusals are logged. Read calls are sampled with
`TOOL_TRACE_READ_SAMPLE` (default `0.1`). Arguments/results contain shapes only, never
document contents, message bodies or confirmation tokens. Confirmer IDs are pseudonymous.
