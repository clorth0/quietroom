"""The one way quietroom opens a SQLite file: race-safe creation and in-place upgrade.

Several processes open the same files at once: the node daemon, and
``quietroom-node --rebaseline/--ack/--promote-anchor/--outbox`` beside it.
Hand-rolled "look at the columns, then ALTER" code raced twice (the store in
#56, the alert outbox after #54), so every module now *declares* its schema
as a :class:`Schema` and :func:`open_db` applies it:

* a busy timeout on the connection, for every later statement too;
* the WAL switch (when the schema asks for it) retried while another opener
  holds the lock, because SQLite can answer it with SQLITE_BUSY at once;
* table creation, column checks and ``ALTER TABLE ... ADD COLUMN`` in one
  ``BEGIN IMMEDIATE`` transaction, so exactly one opener migrates and the
  rest see it done;
* "duplicate column name" treated as harmless, for an opener that skipped
  the lock (an older release still running);
* an optional data upgrade (e.g. backfilling a new column) in the same
  transaction; any failure rolls it all back and closes the connection.

``tests/test_sqlite_discipline.py`` fails if schema DDL or ``sqlite3.connect``
appears anywhere else in the package.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

BUSY_TIMEOUT_S = 30.0


@dataclass(frozen=True, eq=False)
class Schema:
    """A database's shape: what a fresh file gets and what an older one is upgraded with.

    statements: ``CREATE TABLE/INDEX IF NOT EXISTS`` statements, run in order.
    columns: table -> {column: declaration} added in place when missing (shipped
        after the table first did). Use declarations ``ALTER TABLE ADD COLUMN``
        accepts: no PRIMARY KEY/UNIQUE, constant defaults only.
    wal: switch the file to write-ahead logging (readers never block the writer).
    """

    name: str
    statements: tuple[str, ...] = ()
    columns: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    wal: bool = False


Upgrade = Callable[[sqlite3.Connection], Any]

# Test seams (tests/test_sqlitedb.py, tests/test_concurrent_open_forced.py):
# called just before the write lock is taken, and at the check-then-ALTER
# point with the columns found missing. No-ops in production.
_before_lock: Callable[[sqlite3.Connection], None] | None = None
_before_add_columns: Callable[[sqlite3.Connection, str, list[str]], None] | None = None


def _is_lock_error(e: sqlite3.OperationalError) -> bool:
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


def _set_wal(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL").fetchone()


def _existing_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _enable_wal(db: sqlite3.Connection, busy_timeout_s: float) -> None:
    deadline = time.monotonic() + busy_timeout_s
    delay = 0.005
    while True:
        try:
            _set_wal(db)
            return
        except sqlite3.OperationalError as e:
            if not _is_lock_error(e) or time.monotonic() >= deadline:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 0.1)


def _apply(db: sqlite3.Connection, schema: Schema, upgrade: Upgrade | None) -> None:
    # executescript() would COMMIT first, so run statements one by one inside
    # a single write transaction taken up front.
    if _before_lock is not None:
        _before_lock(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        for stmt in schema.statements:
            db.execute(stmt)
        for table, cols in schema.columns.items():
            have = _existing_columns(db, table)
            missing = [c for c in cols if c not in have]
            if not missing:
                continue
            if _before_add_columns is not None:
                _before_add_columns(db, table, missing)
            for col in missing:
                try:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {cols[col]}")
                except sqlite3.OperationalError as e:
                    # Harmless: another opener (one that skipped the lock) added it.
                    if "duplicate column" not in str(e).lower():
                        raise
        if upgrade is not None:
            upgrade(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def open_db(
    path: str | os.PathLike[str],
    schema: Schema,
    *,
    busy_timeout_s: float = BUSY_TIMEOUT_S,
    upgrade: Upgrade | None = None,
    row_factory: Any = None,
) -> sqlite3.Connection:
    """Open ``path`` (``":memory:"`` works), creating or upgrading it to ``schema``.

    Safe to call from any number of threads and processes at once. ``upgrade``
    runs inside the schema transaction, after the columns exist. On any error
    the connection is closed and nothing is half-applied.
    """
    db = sqlite3.connect(str(path), timeout=busy_timeout_s)
    try:
        if row_factory is not None:
            db.row_factory = row_factory
        db.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
        if schema.wal:
            _enable_wal(db, busy_timeout_s)
        _apply(db, schema, upgrade)
    except BaseException:
        db.close()
        raise
    return db
