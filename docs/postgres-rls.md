# Postgres RLS (issue #60)

SQLite remains the default when `DATABASE_URL` is absent. No deployment or data
cutover is part of this change. Use only a disposable local/CI Postgres container
with synthetic data for validation; never the 8788/8789 databases.

Run `python -m app.data_access.rls_migration up` with an owner/admin connection
in `DATABASE_URL`. It creates the actual private model tables and enables and
forces RLS in the same transaction (AC1). Existing `quarantine_markers` tables
are also protected; there is no quarantine-marker model on current main.
Parent-owned conversations and confirm-tool uses resolve ownership through their
binding/token parent. Unrecognized policies or ownership layouts fail migration.

The migration creates two NOLOGIN, NOBYPASSRLS group roles. Provision separate
runtime login roles externally: the app login inherits only `personal_agent_app`,
and the trusted system login inherits only `personal_agent_system`. Neither may own
tables, have administrative privileges, or inherit the owner role (AC5).
The migration grants no role memberships and manages no passwords.

At runtime, `DATABASE_URL` uses the app login; `SYSTEM_DATABASE_URL` uses the
separate system login when background/admin operations require `system_scope`.
`system_scope` never elevates the app database role. ScopedSession selects the
system connection only for a trusted system scope. Each PostgreSQL transaction
sets `app.user_id` using parameterized `set_config(..., true)` (SET LOCAL),
derived exclusively from `current_user()`, including after commit/rollback and
connection reuse (AC2). Changing scope inside an active connection transaction
is rejected. Runtime connections with table ownership or RLS bypass are rejected.

Private policies compare the row owner with
`NULLIF(current_setting('app.user_id', true), '')`; missing/empty identities see
no rows and cannot insert them. USING and WITH CHECK protect reads, updates,
deletes, inserts and ownership changes (AC3). The trusted system role has an
explicit administrative policy. Arbitrary client SQL must not be exposed: the
GUC is a server-installed identity, not an authentication mechanism for SQL clients.

`app.data_access.public.PUBLIC_TABLES` is the authoritative public registry and
is empty today. Register future shared tables during trusted startup/migration;
the owner must create them before running the RLS migration. The migration grants
app SELECT and system SELECT/INSERT/UPDATE/DELETE, and revokes PUBLIC access (AC4).
Saved profile, memory and draft records remain private. CI uses four synthetic shared reference
tables to verify grants without inventing production public stores.

Postgres schema setup is migration-only; SQLite lazy schema upgrades are retained.
Postgres speculative business writes use one transaction through guard completion,
rolling back on failure. Audit and LangGraph checkpoint stores remain local SQLite
with their existing AC4 guards; this ticket does not migrate those stores.

`python -m app.data_access.rls_migration down` removes this migration's policies
and disables RLS, preserving data/schema/roles. It first revokes runtime table and
sequence grants so rollback cannot expose private rows. PUBLIC schema CREATE stays
revoked. Run `up` again to restore service; `down` is not a production cutover plan.

CI's `postgres-rls` job uses an ephemeral Postgres 16 service and real non-owner
login connections. `RLS_TEST_DATABASE_URL` must point to `personal_agent_rls_test` on a
local/service host, with no URL query overrides. `RLS_TEST_REQUIRED=1` makes missing
configuration, driver, or service a failure in CI. Without Postgres, local tests
skip only this integration module as authorized. The same workflow runs full
SQLite pytest and golden mock gates (AC6). Postgres proof comes from CI, not the
offline wiring tests.
