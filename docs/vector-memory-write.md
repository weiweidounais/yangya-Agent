# Vector memory write path (phase B)

`MEMORY_VECTOR_ENABLED` remains off by default. For users included in rollout, the existing
summary worker retains every submitted turn, propagates its owner's `user_scope`,
redacts before LLM calls, and calls the gateway's fast `memory_extract` and
`memory_reconcile` tasks. Strict JSON/schema failures abort the whole batch.
Gateway usage retains those task names; a background token meter charges the
existing per-user cost ledger, including calls made before a failed batch.

Extraction and embeddings run before acquiring the memory transaction lock.
Reconciliation sees only the owner's five nearest valid facts. Candidate IDs
are validated before persistence and candidate validity is checked again under
the lock. Facts, profile summary, and summary event commit together, subject to
the existing memory revision check. UPDATE invalidates the old row and adds a
replacement; DELETE only sets `valid_until`. Failures use the summary worker's
existing retry policy. No vector facts are added to chat context in this phase.

User-stated facts are confirmed. Inferred facts remain pending and cannot
invalidate confirmed facts. After commit, `write_path.on_pending_fact(fact)`
delegates to `ask_cards.on_pending_memory_fact(fact)` under the owner's scope.
The existing ask-first flow can subscribe with
`ask_cards.set_pending_fact_handler(handler)` and unsubscribe with `None`.
The default adapter does nothing. A handler receives the committed DAL fact,
including its ID, owner, text, and `confirmed=False`; it can queue an existing
ask card. Notification is not consent and does not confirm the fact. Handler
failures are logged after commit and do not retry the committed batch. This
phase adds no UI, delivery queue, or confirmation-response endpoint.

## Phase D: migration and rollout

Run from the repository root against an explicitly selected, backed-up database
(`DATA_DIR` selects the existing SQLite directory). Start with synthetic data;
these commands are operator instructions, not an automatic production migration.

```sh
python scripts/migrate_memory_facts.py --dry-run --user synthetic-A --limit 1
python scripts/migrate_memory_facts.py --apply --user synthetic-A --limit 1
```

Dry-run is the default: no schema/data writes and no embedding calls. Repeat
`--user` to select owners, or omit it to enumerate owner IDs through the DAL.
`--limit` caps users, not facts. JSON lines report only SHA-256 user hashes and
`would_add`, `skip`, `existing`, `added`, `failed`. `skip` counts duplicate or
ineligible source items; `existing` counts candidates already covered by facts
or receipts. Exit status 1 means a user failed or enumeration failed. Errors
never include exception messages or source text. Retry the same command to
resume; one user's failure does not stop other users.

Apply uses the existing embedding client independently of the live rollout
gate, so backfill can finish with vector memory disabled. Embedding calls happen
before the per-user write transaction. Source changes during embedding cause
that user's batch to fail for retry. Profile fields are confirmed; dialogue
summaries and all legacy event summaries remain pending because their origin
cannot reliably establish user confirmation. Raw résumé bodies and obsolete
`previous_cities` are excluded. Facts retain event IDs; the scoped
`memory_migration_receipts` table records `migration:profile:<field>` or
`migration:event:<id>` provenance and stable user/source/normalized-text keys.
Receipts survive invalidation, replacement and deletion, preventing an unchanged
legacy source from recreating an old fact. They contain no fact text.

After reviewing migration counts, set `MEMORY_VECTOR_ENABLED=1` and
`MEMORY_VECTOR_ALLOW_USERS` to a comma-separated list of trusted user IDs for
the initial cohort. Then increase `MEMORY_VECTOR_ROLLOUT_PERCENT` from 0 to a
small integer percentage, monitor quality/errors, and increase toward 100.
The percentage bucket is SHA-256(user ID), first eight bytes as a big-endian
integer modulo 10000; users remain in their cohort across restarts. The allowlist
is additive. Invalid percentages fail closed for users outside the allowlist.
Global off disables live vector memory for everyone. Global on with percentage
0 (the default) and an empty allowlist enables nobody. Only server `user_scope`
identity controls eligibility; request parameters cannot opt another user in.
