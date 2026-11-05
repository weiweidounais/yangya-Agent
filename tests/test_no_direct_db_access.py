"""AC4: keep raw database capabilities behind app.data_access.

This is an AST dependency guard, not a Python sandbox. It follows imports,
assignments, context-manager targets, annotations and literal getattr/importlib
aliases within lexical scopes. Unknown SQL passed to execute fails closed;
SQLAlchemy ORM expressions on DAL sessions remain valid. Runtime ownership is
still enforced by ScopedSession and the stage 1–3 isolation tests.
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Exact file + symbol only. An exception does not exempt siblings or nested
# functions. Keep business reads/writes out of this list.
ALLOWLIST = {
    ("app/db.py", "ENGINE"): "Bootstrap the shared SQLite engine used by the DAL.",
    ("app/db.py", "init_db"): "Create schema on the existing transaction connection to avoid self-deadlock.",
    ("app/db.py", "memory_transaction"): "Acquire SQLite's BEGIN IMMEDIATE transaction lock; no raw row access.",
    ("app/audit_db.py", "AUDIT_ENGINE"): "Bootstrap the independent audit database so audit writes survive business rollback.",
    ("app/audit_db.py", "ensure_audit_table"): "Migrate legacy audit tables once into the independent audit database.",
    ("app/agent/harness/traces.py", "_ensure"): "Upgrade only the audit trace schema on its configured bind.",
    ("app/agent/cost_ledger.py", "_ensure"): "Create the plan schema on the active transaction connection without self-deadlock.",
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    symbol: str
    rule: str

    def __str__(self):
        return f"{self.path}:{self.line} ({self.symbol}): {self.rule}"


def _dal(name):
    return name == "app.data_access" or name.startswith("app.data_access.")


def _database_module(name):
    return name.split(".")[0] in {"sqlite3", "sqlalchemy"}


def _unscoped(name):
    leaf = name.rsplit(".", 1)[-1]
    return (leaf == "SessionLocal" and not _dal(name)) or leaf in {
        "Session", "sessionmaker", "scoped_session", "AsyncSession",
        "async_sessionmaker",
    }


class DatabaseScanner(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.bindings = {}
        self.definitions = {}
        self.resolving = set()
        self.symbol = "<module>"
        self.violations = set()

    def report(self, node, rule):
        self.violations.add(Violation(self.path, node.lineno, self.symbol, rule))

    def attribute(self, base, name):
        if name == "bind" and (base.startswith(("@session", "@unscoped")) or
                               _unscoped(base) or base.endswith(".ScopedSession")):
            return "@engine"
        return base + "." + name

    def resolve(self, node):
        """Small, conservative provenance analysis; never execute source code."""
        if isinstance(node, ast.Name):
            values = set(self.bindings.get(node.id, set()))
            if node.id in self.resolving:
                return values
            self.resolving.add(node.id)
            try:
                for value in self.definitions.get(node.id, []):
                    values |= self.resolve(value)
            finally:
                self.resolving.remove(node.id)
            return values or {node.id}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {"str:" + node.value}
        if isinstance(node, ast.JoinedStr):
            values = {"str:"}
            for part in node.values:
                parts = self.resolve(part.value if isinstance(part, ast.FormattedValue) else part)
                values = {a + b[4:] for a in values for b in parts if b.startswith("str:")}
            return values or {"str:"}
        if isinstance(node, ast.Attribute):
            return {self.attribute(name, node.attr) for name in self.resolve(node.value)}
        if isinstance(node, ast.IfExp):
            return self.resolve(node.body) | self.resolve(node.orelse)
        if isinstance(node, ast.BinOp):
            left, right = self.resolve(node.left), self.resolve(node.right)
            if isinstance(node.op, ast.Add):
                return {"str:" + a[4:] + b[4:] for a in left for b in right
                        if a.startswith("str:") and b.startswith("str:")}
            if any(a.startswith("str:") for a in left):
                return {"str:"}
        if not isinstance(node, ast.Call):
            return set()
        funcs = self.resolve(node.func)
        result = set()
        for name in funcs:
            base, _, leaf = name.rpartition(".")
            if name in {"getattr", "builtins.getattr"} and len(node.args) >= 2:
                result |= {self.attribute(base, attr[4:])
                           for base in self.resolve(node.args[0])
                           for attr in self.resolve(node.args[1]) if attr.startswith("str:")}
            elif name in {"importlib.import_module", "__import__", "builtins.__import__"}:
                args = node.args[:1] or [kw.value for kw in node.keywords if kw.arg == "name"]
                if args:
                    result |= {value[4:] for value in self.resolve(args[0]) if value.startswith("str:")}
            elif leaf in {"create_engine", "create_async_engine", "engine_from_config"} and (
                    _database_module(name) or _dal(name) or "." not in name):
                result.add("@engine")
            elif _dal(name) and leaf in {"make_session_factory", "session_factory"}:
                result.add("app.data_access.session.SessionLocal")
            elif _dal(name) and leaf in {"SessionLocal", "ScopedSession", "memory_session", "migration_session"}:
                result.add("@session")
            elif _unscoped(name):
                result.add("@unscoped")
            elif leaf in {"connect", "raw_connection", "connection"}:
                result.add("@connection")
            elif leaf == "get_bind":
                result.add("@engine")
            elif leaf == "begin":
                result.add("@session" if _dal(name) or name.startswith("@session.") else "@connection")
            elif (leaf in {"execute", "scalars"} and
                  base in {"@session", "@unscoped", "@connection", "@engine"}) or name == "@result.scalars":
                result.add("@result")
            elif name.startswith("str:") or name == "str":
                result.add("str:")
            elif name.startswith(("sqlalchemy.", "@orm.")):
                result.add("@orm")
            else:
                result.add(name + "()")
        return result

    def bind(self, target, values):
        if isinstance(target, ast.Name):
            # Union preserves dangerous origins across branches/reassignments.
            self.bindings.setdefault(target.id, set()).update(values)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.bind(item, values)

    def scope_nodes(self, body):
        """Walk a lexical scope without leaking a sibling function's imports."""
        for node in body:
            yield node
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                yield from self.scope_nodes(ast.iter_child_nodes(node))

    def collect(self, body):
        nodes = list(self.scope_nodes(body))
        # Imports may occur after a function definition; resolve assignments
        # lazily, with a recursion guard for cyclic/loop-carried aliases.
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                    self.bindings.pop(name, None)
                    self.definitions.pop(name, None)
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.bind(ast.Name(id=alias.asname or alias.name.split(".")[0]),
                              {alias.name if alias.asname else alias.name.split(".")[0]})
            elif isinstance(node, ast.ImportFrom):
                module = self.import_module(node)
                for alias in node.names:
                    self.bind(ast.Name(id=alias.asname or alias.name), {module + "." + alias.name})
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    self.define(target, node.value)
            elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
                self.definitions.setdefault(node.optional_vars.id, []).append(node.context_expr)

    def define(self, target, value):
        if isinstance(target, ast.Name):
            self.definitions.setdefault(target.id, []).append(value)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            for item, expression in zip(target.elts, value.elts):
                self.define(item, expression)

    def import_module(self, node):
        if not node.level:
            return node.module or ""
        package = self.path.removesuffix(".py").split("/")[:-1]
        return ".".join(package[:len(package) - node.level + 1] + ([node.module] if node.module else []))

    def visit_Module(self, node):
        self.collect(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        previous, definitions, symbol = self.bindings, self.definitions, self.symbol
        self.bindings = {key: set(value) for key, value in previous.items()}
        self.definitions = {key: list(value) for key, value in definitions.items()}
        self.symbol = node.name if symbol == "<module>" else symbol + "." + node.name
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in args:
            self.bindings[arg.arg] = self.resolve(arg.annotation) or {arg.arg}
            self.definitions.pop(arg.arg, None)
        for arg, default in zip(node.args.posonlyargs + node.args.args, [None] * (
                len(node.args.posonlyargs + node.args.args) - len(node.args.defaults)) + node.args.defaults):
            self.bind(ast.Name(id=arg.arg), self.resolve(default))
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            self.bind(ast.Name(id=arg.arg), self.resolve(default))
        self.collect(node.body)
        self.generic_visit(node)
        self.bindings, self.definitions, self.symbol = previous, definitions, symbol

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous, definitions, symbol = self.bindings, self.definitions, self.symbol
        self.bindings = {key: set(value) for key, value in previous.items()}
        self.definitions = {key: list(value) for key, value in definitions.items()}
        self.symbol = node.name if symbol == "<module>" else symbol + "." + node.name
        if any(_unscoped(name) for base in node.bases for name in self.resolve(base)):
            self.report(node, "unscoped-session")
        self.collect(node.body)
        self.generic_visit(node)
        self.bindings, self.definitions, self.symbol = previous, definitions, symbol

    def visit_Assign(self, node):
        symbol = self.symbol
        if symbol == "<module>" and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.symbol = node.targets[0].id
        self.generic_visit(node)
        self.symbol = symbol

    def visit_Import(self, node):
        if any(alias.name.split(".")[0] == "sqlite3" for alias in node.names):
            self.report(node, "sqlite-import")

    def visit_ImportFrom(self, node):
        module = self.import_module(node)
        if module.split(".")[0] == "sqlite3":
            self.report(node, "sqlite-import")
        if any(alias.name == "*" for alias in node.names) and (
                _database_module(module) or module in {"app", "app.db"}):
            self.report(node, "database-star-import")

    def visit_Call(self, node):
        for name in self.resolve(node.func):
            base, _, leaf = name.rpartition(".")
            if name.startswith("sqlite3."):
                self.report(node, "sqlite-call")
            if (_database_module(name) or _dal(name) or not base) and leaf in {
                    "create_engine", "create_async_engine", "engine_from_config"}:
                self.report(node, "engine-construction")
            if _unscoped(name) or name.startswith("@unscoped") or (
                    leaf == "begin" and _unscoped(base)):
                self.report(node, "unscoped-session")
            if leaf in {"text", "TextClause"} and (_database_module(name) or not base):
                self.report(node, "text-sql")
            if leaf == "Connection" and (_database_module(name) or not base):
                self.report(node, "raw-connection")
            if leaf in {"connection", "raw_connection", "exec_driver_sql", "cursor"}:
                self.report(node, "raw-connection")
            engine = base.startswith("@engine") or base.rsplit(".", 1)[-1].lower() in {"engine", "audit_engine", "bind"}
            if leaf in {"connect", "begin"} and engine:
                self.report(node, "raw-connection")
            if leaf in {"execute", "executemany", "executescript", "scalar", "scalars"}:
                args = node.args[:1] or [kw.value for kw in node.keywords if kw.arg in {"statement", "sql"}]
                values = self.resolve(args[0]) if args else set()
                # Result.scalars(index)/scalar() consume an already executed query.
                consumes_result = base == "@result" and leaf in {"scalar", "scalars"}
                if values != {"@orm"} and not consumes_result:
                    self.report(node, "string-or-unknown-sql")
                if (base.startswith(("@connection", "@engine")) or engine or
                        base.rsplit(".", 1)[-1] == "Connection"):
                    self.report(node, "raw-connection")
            if name in {"importlib.import_module", "__import__", "builtins.__import__"}:
                if any(_database_module(value) or value == "app.db" for value in self.resolve(node)):
                    self.report(node, "dynamic-database-import")
        self.generic_visit(node)


def scan_source(source, path="app/example.py"):
    scanner = DatabaseScanner(path)
    scanner.visit(ast.parse(dedent(source), filename=path))
    return sorted(scanner.violations, key=lambda item: (item.line, item.rule))


def scan_app(root=ROOT):
    violations = []
    for path in sorted((root / "app").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("app/data_access/"):
            continue
        violations.extend(scan_source(path.read_text(encoding="utf-8"), relative))
    return violations


def test_no_direct_db_access():
    violations = scan_app()
    unexpected = [item for item in violations if (item.path, item.symbol) not in ALLOWLIST]
    assert not unexpected, "Database access must go through app.data_access:\n" + "\n".join(map(str, unexpected))
    used = {(item.path, item.symbol) for item in violations}
    assert ALLOWLIST.keys() <= used, "Remove stale allowlist entries: " + str(ALLOWLIST.keys() - used)
    assert all(reason.strip() and "\n" not in reason for reason in ALLOWLIST.values())


@pytest.mark.parametrize("source,rule", [
    ("import sqlite3", "sqlite-import"),
    ("import sqlite3 as s; s.connect(':memory:')", "sqlite-call"),
    ("from sqlite3 import connect as c; c(':memory:')", "sqlite-call"),
    ("from sqlalchemy import create_engine as ce; ce('sqlite://')", "engine-construction"),
    ("import sqlalchemy as sa; sa.create_engine('sqlite://')", "engine-construction"),
    ("create_engine('sqlite://')", "engine-construction"),
    ("from app.data_access.staging import create_engine as ce; ce('sqlite://')", "engine-construction"),
    ("from sqlalchemy import create_engine; factory = create_engine; factory('sqlite://')", "engine-construction"),
    ("from sqlalchemy import create_engine as ce; factory, = (ce,); factory('sqlite://')", "engine-construction"),
    ("from app.db import ENGINE as e; e.connect()", "raw-connection"),
    ("from app import db; db.ENGINE.begin()", "raw-connection"),
    ("from sqlalchemy.engine import Connection as C; C(engine)", "raw-connection"),
    ("from sqlalchemy.engine import Connection as C\ndef query(conn: C):\n conn.execute(select(User))", "raw-connection"),
    ("from sqlalchemy.engine.base import Connection as C\nfrom sqlalchemy import select\ndef query(conn: C):\n conn.execute(select(User))", "raw-connection"),
    ("from app.db import ENGINE as e\nfrom sqlalchemy import select\ne.execute(select(User))", "raw-connection"),
    ("session.connection().execute(select(User))", "raw-connection"),
    ("session.get_bind().connect()", "raw-connection"),
    ("session.bind.raw_connection()", "raw-connection"),
    ("from app.data_access.session import SessionLocal\nfrom sqlalchemy import select\nwith SessionLocal() as s:\n s.bind.connect().scalar(select(User))", "raw-connection"),
    ("conn.exec_driver_sql('SELECT * FROM user_profiles')", "raw-connection"),
    ("from sqlalchemy import text as t; t('SELECT * FROM user_profiles')", "text-sql"),
    ("import sqlalchemy as sa; sa.text('UPDATE shared_reference SET name=1')", "text-sql"),
    ("session.execute('SELECT * FROM user_profiles')", "string-or-unknown-sql"),
    ("session.execute(statement='DELETE FROM shared_reference')", "string-or-unknown-sql"),
    ("session.scalar('SELECT * FROM user_profiles')", "string-or-unknown-sql"),
    ("session.scalars(statement='SELECT * FROM user_profiles')", "string-or-unknown-sql"),
    ("sql = 'SELECT * FROM user_profiles'; query = sql; session.execute(query)", "string-or-unknown-sql"),
    ("session.execute(f'SELECT * FROM {table}')", "string-or-unknown-sql"),
    ("session.execute('SELECT * FROM {}'.format(table))", "string-or-unknown-sql"),
    ("def query(sql):\n session.execute(sql)", "string-or-unknown-sql"),
    ("from sqlalchemy.orm import Session as S; S(engine)", "unscoped-session"),
    ("from app.data_access.session import Session as S; S(engine)", "unscoped-session"),
    ("import sqlalchemy.orm as orm; orm.Session(engine)", "unscoped-session"),
    ("from sqlalchemy.orm import sessionmaker as sm; sm(bind=engine)()", "unscoped-session"),
    ("from sqlalchemy.orm import Session as S\nclass Bypass(S):\n pass", "unscoped-session"),
    ("from sqlalchemy.orm import Session as S\ndef query(*, factory=S):\n factory(engine)", "unscoped-session"),
    ("from app.db import SessionLocal as S; S()", "unscoped-session"),
    ("from .db import SessionLocal as S; S.begin()", "unscoped-session"),
    ("import app.db as db; factory = db.SessionLocal; factory()", "unscoped-session"),
    ("import sqlite3 as s; getattr(s, 'connect')(':memory:')", "sqlite-call"),
    ("import sqlalchemy as sa; name = 'create_engine'; getattr(sa, name)('sqlite://')", "engine-construction"),
    ("from app.db import ENGINE as e; c = getattr(e, 'connect'); c()", "raw-connection"),
    ("import importlib as il; il.import_module('sqlite3')", "dynamic-database-import"),
    ("from importlib import import_module as im; im('sqlalchemy.orm')", "dynamic-database-import"),
    ("from importlib import import_module as im; im(name='sqlite3')", "dynamic-database-import"),
    ("import importlib; name = 'sql' + 'alchemy'; importlib.import_module(name)", "dynamic-database-import"),
    ("import importlib; importlib.import_module(f'sqlite3')", "dynamic-database-import"),
    ("import importlib; name = 'sqlalchemy'; importlib.import_module(f'{name}.orm')", "dynamic-database-import"),
    ("__import__('sqlite3').connect(':memory:')", "dynamic-database-import"),
    ("from sqlalchemy import *", "database-star-import"),
    ("def query():\n from sqlalchemy import create_engine as ce\n ce('sqlite://')", "engine-construction"),
    ("def query():\n ce('sqlite://')\nfrom sqlalchemy import create_engine as ce", "engine-construction"),
])
def test_scanner_rejects_bypasses(source, rule):
    assert rule in {item.rule for item in scan_source(source)}


@pytest.mark.parametrize("bind", ["s.bind", "s.get_bind()", "getattr(s, 'bind')"])
@pytest.mark.parametrize("operation", [
    "engine.connect()", "engine.begin()", "engine.raw_connection()",
    "engine.execute(select(User))", "engine.scalar(select(User))",
    "engine.scalars(select(User))", "engine.connect().scalar(select(User))",
    "engine.connect().scalars(select(User))",
    "engine.execution_options(stream_results=True).connect()",
])
def test_scanner_rejects_session_bound_engine(bind, operation):
    source = f"""
    from app.data_access.session import SessionLocal
    from sqlalchemy import select
    with SessionLocal() as s:
        engine = {bind}
        {operation}
    """
    assert "raw-connection" in {item.rule for item in scan_source(source)}


@pytest.mark.parametrize("method", ["scalar", "scalars"])
def test_scanner_rejects_raw_connection_scalar_queries(method):
    source = f"""
    from sqlalchemy.engine import Connection as C
    from sqlalchemy import select
    def query(conn: C):
        query = getattr(conn, '{method}')
        return query(select(User))
    """
    assert "raw-connection" in {item.rule for item in scan_source(source)}


@pytest.mark.parametrize("source", [
    """
    from sqlalchemy import select, update
    from app.data_access.session import SessionLocal as sessions
    with sessions() as session:
        query = select(User).where(User.id == owner)
        session.execute(query)
        session.execute(update(User).values(name='new'))
        session.scalar(select(User))
        session.scalars(select(User))
        session.execute(select(User)).scalar()
    """,
    """
    import app.data_access.session as dal
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    def read(existing: Session):
        with dal.memory_session(existing) as session:
            return session.execute(select(User)).scalars().all()
    """,
    """
    from sqlalchemy import select
    from app.data_access.session import SessionLocal
    with SessionLocal() as session:
        query = select(User)
        if owner:
            query = query.where(User.id == owner)
        session.execute(query.order_by(User.id))
    """,
    """
    from app.data_access.session import session_factory
    from sqlalchemy import select
    factory = session_factory()
    with factory.begin() as session:
        session.execute(select(User))
    """,
    """
    from app.channels import outbound
    outbound.connect(user, platform)
    # import sqlite3; sqlite3.connect('ignored')
    documentation = "session.execute('SELECT * FROM user_profiles')"
    """,
])
def test_scanner_allows_dal_orm_and_non_database_code(source):
    assert scan_source(source) == []


@pytest.mark.parametrize("operation", [
    "s.execute(select(User)).scalars(0).all()",
    "result.scalars(0).all()",
    "result.scalar()",
    "result.all()",
    "consume = result.scalars; consume(0).all()",
    "s.scalars(select(User)).all()",
])
def test_scanner_allows_dal_result_consumption(operation):
    source = f"""
    from app.data_access.session import SessionLocal
    from sqlalchemy import select
    with SessionLocal() as s:
        result = s.execute(select(User))
        {operation}
    """
    assert scan_source(source) == []


@pytest.mark.parametrize("operation", [
    "s.scalars(0)",
    "s.scalar(0)",
    "s.scalars()",
    "s.scalar()",
    "s.execute('SELECT * FROM user_profiles').scalars(0).all()",
    "result = s; result.scalars(0)",
    "consume = s.scalars; consume(0)",
])
def test_scanner_rejects_sql_despite_result_consumption(operation):
    source = f"""
    from app.data_access.session import SessionLocal
    from sqlalchemy import select
    with SessionLocal() as s:
        result = s.execute(select(User))
        {operation}
    """
    assert "string-or-unknown-sql" in {item.rule for item in scan_source(source)}


def test_allowlist_is_symbol_specific():
    source = """
    from sqlalchemy import create_engine
    ENGINE = create_engine('sqlite://')
    def read_profile():
        return ENGINE.connect()
    """
    violations = scan_source(source, "app/db.py")
    unexpected = [item for item in violations if (item.path, item.symbol) not in ALLOWLIST]
    assert [(item.symbol, item.rule) for item in unexpected] == [("read_profile", "raw-connection")]


def test_sibling_import_cannot_launder_an_unscoped_session():
    source = """
    from sqlalchemy.orm import Session as S
    def allowed():
        from app.data_access.session import SessionLocal as S
        return S()
    def bypass():
        return S(engine)
    """
    assert [(item.symbol, item.rule) for item in scan_source(source)] == [("bypass", "unscoped-session")]


def test_scan_app_exempts_only_the_dal_directory(tmp_path):
    for name in ("data_access/allowed.py", "data_access_lookalike/bypass.py", "nested/bypass.py"):
        path = tmp_path / "app" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import sqlite3\n", encoding="utf-8")
    assert {item.path for item in scan_app(tmp_path)} == {
        "app/data_access_lookalike/bypass.py", "app/nested/bypass.py",
    }
