"""Static check: schema DDL and sqlite3.connect live only in quietroom/sqlitedb.py.

A module may *declare* its schema (``Schema(...)``: CREATE ... IF NOT EXISTS
statements and columns to add), but only ``quietroom.sqlitedb.open_db`` runs
it, under the lock. This is the structural fix for the open/migrate race that
happened twice (#56 in the store, again in the alert outbox from #54): a new
module cannot hand-roll ``ALTER TABLE`` again without this test failing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import quietroom

PKG = Path(quietroom.__file__).parent
HELPER = PKG / "sqlitedb.py"
DDL = re.compile(
    r"\b(ALTER\s+TABLE|CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER)|"
    r"DROP\s+(TABLE|INDEX|VIEW|TRIGGER)|PRAGMA\s+journal_mode)\b",
    re.IGNORECASE,
)
RAW_CALLS = {"connect", "executescript"}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def violations(source: str, filename: str = "<src>") -> list[str]:
    """Schema DDL or raw sqlite3 opens outside a ``Schema(...)`` declaration."""
    tree = ast.parse(source, filename)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def inside_schema(node: ast.AST) -> bool:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.Call) and _call_name(node.func) == "Schema":
                return True
        return False

    def is_docstring(node: ast.AST) -> bool:
        p = parents.get(node)
        return isinstance(p, ast.Expr)

    # every name the sqlite3 module is bound to (``import sqlite3 as lite``)
    modules = {"sqlite3"} | {
        a.asname
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
        if a.name == "sqlite3" and a.asname
    }
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            if any(a.name in RAW_CALLS or a.name == "*" for a in node.names):
                out.append(f"{filename}:{node.lineno}: sqlite3 connect imported directly")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if DDL.search(node.value) and not is_docstring(node) and not inside_schema(node):
                out.append(f"{filename}:{node.lineno}: schema DDL outside Schema(...)")
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if DDL.search(text) and not inside_schema(node):
                out.append(f"{filename}:{node.lineno}: schema DDL outside Schema(...)")
        elif isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in RAW_CALLS
                and (f.attr == "executescript" or _call_name(f.value) in modules)
            ):
                out.append(f"{filename}:{node.lineno}: {f.attr}() outside quietroom/sqlitedb.py")
    return sorted(set(out))


def test_no_module_runs_schema_ddl_or_opens_sqlite_outside_the_helper():
    found = []
    for path in sorted(PKG.rglob("*.py")):
        if path == HELPER:
            continue
        found += violations(path.read_text(encoding="utf-8"), str(path.relative_to(PKG.parent)))
    assert found == [], "\n".join(found)


def test_the_helper_exists_and_is_where_the_ddl_runs():
    src = HELPER.read_text(encoding="utf-8")
    assert "BEGIN IMMEDIATE" in src and "ALTER TABLE" in src


# The check must actually fire; prove it on known-bad snippets.
BAD = {
    "alter": 'db.execute("ALTER TABLE outbox ADD COLUMN x TEXT")',
    "alter-fstring": 'db.execute(f"ALTER TABLE outbox ADD COLUMN {col} {decl}")',
    "create": 'db.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER)")',
    "create-index": 'SQL = "create unique index i on t(a)"',
    "drop": 'db.execute("DROP TABLE t")',
    "journal": 'db.execute("PRAGMA journal_mode=WAL")',
    "connect": "import sqlite3\ndb = sqlite3.connect('x.db')",
    "executescript": "db.executescript(SCHEMA)",
    "from-import-connect": "from sqlite3 import connect\ndb = connect('x.db')",
    "aliased-module": "import sqlite3 as lite\ndb = lite.connect('x.db')",
    "schema-constant": 'SCHEMA = """\nCREATE TABLE IF NOT EXISTS t(id INTEGER);\n"""',
}
GOOD = {
    "declared": 'S = Schema("s", statements=("CREATE TABLE IF NOT EXISTS t(id INTEGER)",))',
    "declared-qualified": 'S = sqlitedb.Schema("s", columns={"t": {"x": "TEXT"}},'
    ' statements=("CREATE INDEX IF NOT EXISTS i ON t(x)",))',
    "dml": 'db.execute("UPDATE t SET a=1"); db.execute("INSERT INTO t VALUES(1)")',
    "docstring": 'def f():\n    """Upgrades in place with ALTER TABLE under the lock."""',
    "prose": 'msg = "the table was altered"',
    "other-connect": "import socket\ns = socket.create_connection(('h', 1)); c.connect()",
    "sqlite-types": "from sqlite3 import Connection, OperationalError",
}


def test_the_check_flags_every_bad_pattern():
    for name, src in BAD.items():
        assert violations(src), name


def test_the_check_allows_declarations_dml_and_docs():
    for name, src in GOOD.items():
        assert violations(src) == [], name
