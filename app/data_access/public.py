"""Public-store policy. Production currently has no public database tables.

Every user-owned record (profile, memory, drafts, schedules) is PRIVATE.
Register future shared tables explicitly during trusted startup/migrations.
Registration protects a table name across schemas, including reflected/Core
tables: qualifying the default schema must not bypass the write guard.
"""
from types import MappingProxyType

from app.data_access import current_user


# Existing shared references, not user-owned records or fictitious DB stores.
# Writers are deployment code/imports; no user-input learning or editing API.
SHARED_REFERENCE_STORES = MappingProxyType({
    "prompt_templates": ("app/prompts/*/*.md", "app.prompts._loaded",
                         "app.agent.harness.leak._prompts", "app.agent.harness.prompt_leak._cache"),
    "skills": ("app/agent/skills/registry.json", "app/agent/skills/packages/",
               "app.agent.skills.registry"),
    "reference_vocabularies": ("app.agent.budget_tiers.TASK_DIFFICULTY",),
    "tool_and_adapter_catalogs": ("app.agent.tool_registry._REGISTRY",
                                  "app.channels.registry._ADAPTERS", "app.channels.registry._ALIASES"),
})
# No public knowledge DB exists. Web sources are fetched per request and never stored as shared data.
# Gateway config/pricing caches derive from deployment config, not user content.
# Usage rings and scheduler status are operational data, exposed as de-identified
# metrics; feedback/traces/audit lists are admin-only (see tests/test_ac4_s3_public.py).


class PublicWriteForbidden(PermissionError):
    code = "public_write_forbidden"
    http_status = 403

    def __init__(self):
        super().__init__("Public stores require system_scope for writes")


PUBLIC_TABLES: frozenset[str] = frozenset()


def require_public_writer():
    if current_user() is not None:
        raise PublicWriteForbidden()


def register_public_table(table):
    """Classify a SQLAlchemy Table; never callable as a user-owned operation."""
    from app.data_access.session import _PRIVATE_TABLES

    require_public_writer()
    if table.name in _PRIVATE_TABLES:
        raise ValueError("Private tables cannot be registered as public")
    global PUBLIC_TABLES
    PUBLIC_TABLES = PUBLIC_TABLES | {table.name}


def is_public_table(table):
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.selectable import TableClause
    return bool(PUBLIC_TABLES) and table is not None and any(
        isinstance(node, TableClause) and node.name in PUBLIC_TABLES
        for node in visitors.iterate(table))


def check_public_model_write(model):
    if is_public_table(getattr(model, "__table__", None)):
        require_public_writer()


def check_public_statement(statement):
    from sqlalchemy.sql.dml import Insert, Update, Delete
    from sqlalchemy.sql.elements import TextClause
    from sqlalchemy.sql.selectable import CompoundSelect, Select, TextualSelect

    if not PUBLIC_TABLES:
        return
    pending = [(statement, False)]
    while pending:
        clause, in_select = pending.pop()
        if isinstance(clause, (Select, CompoundSelect)):
            in_select = True
        elif isinstance(clause, (Insert, Update, Delete)):
            in_select = False
            if is_public_table(clause.table):
                require_public_writer()
        elif isinstance(clause, TextualSelect):
            # text(...).columns(...) is still opaque SQL, even inside a SELECT.
            in_select = False
        elif isinstance(clause, TextClause) and not in_select:
            if clause.text.strip().upper() == "BEGIN IMMEDIATE":
                continue  # Scoped memory_session's transaction lock, no row access.
            # Text has no reliable table/write metadata. Once public tables exist,
            # users must use inspectable ORM/Core statements. Text fragments
            # within a verified SELECT (e.g. ORDER BY/WHERE) are safe to read.
            require_public_writer()
        pending.extend((child, in_select) for child in clause.get_children())
