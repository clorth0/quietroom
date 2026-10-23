"""quietroom.sqlitedb.open_db: the one race-safe way to open and migrate a SQLite file.

Every database in the repo (node store, alert outbox, fleet outbox) is opened
through it. Each safeguard has its own deterministic test, driven by hooks
inside the helper rather than by luck:

* BEGIN IMMEDIATE: schema work holds the write lock (another writer is refused);
* duplicate column is harmless: an ALTER that finds the column already added
  (by an opener that skipped the lock, e.g. an older release) is not an error;
* WAL switch retried while another opener holds the lock;
* two openers forced into the check-then-ALTER window together never fail.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from quietroom import sqlitedb
from quietroom.sqlitedb import Schema, open_db

OLD = "CREATE TABLE t(id INTEGER PRIMARY KEY, a TEXT); INSERT INTO t(a) VALUES('kept');"
SCHEMA = Schema(
    "test",
    statements=("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, a TEXT)",),
    columns={"t": {"b": "TEXT DEFAULT ''", "c": "REAL"}},
)


def _old(path):
    db = sqlite3.connect(path)
    db.executescript(OLD)
    db.close()


def _cols(path, table="t"):
    db = sqlite3.connect(path)
    try:
        return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    finally:
        db.close()


def test_creates_a_new_file_and_upgrades_an_old_one(tmp_path):
    new, old = tmp_path / "new.db", tmp_path / "old.db"
    open_db(new, SCHEMA).close()
    assert _cols(new) == ["id", "a", "b", "c"]
    _old(old)
    db = open_db(old, SCHEMA)
    assert db.execute("SELECT a, b, c FROM t").fetchall() == [("kept", "", None)]
    db.close()
    open_db(old, SCHEMA).close()  # idempotent
    assert _cols(old) == ["id", "a", "b", "c"]


def test_upgrade_callback_runs_in_the_same_transaction(tmp_path):
    p = tmp_path / "x.db"
    _old(p)
    seen = []

    def upgrade(db):
        seen.append(db.in_transaction)
        db.execute("UPDATE t SET c=42 WHERE c IS NULL")

    db = open_db(p, SCHEMA, upgrade=upgrade)
    assert seen == [True]
    assert db.execute("SELECT c FROM t").fetchall() == [(42.0,)]
    db.close()


def test_a_failing_upgrade_rolls_everything_back_and_closes(tmp_path):
    p = tmp_path / "x.db"
    _old(p)

    def boom(db):
        raise RuntimeError("backfill failed")

    with pytest.raises(RuntimeError, match="backfill failed"):
        open_db(p, SCHEMA, upgrade=boom)
    assert _cols(p) == ["id", "a"]  # the ALTERs were rolled back with it


def test_busy_timeout_is_set_on_the_connection(tmp_path):
    db = open_db(tmp_path / "x.db", SCHEMA, busy_timeout_s=7.5)
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 7500
    db.close()


def test_schema_work_holds_the_write_lock(tmp_path, monkeypatch):
    """Safeguard 1, BEGIN IMMEDIATE: nobody else can write while columns are added."""
    p = tmp_path / "x.db"
    _old(p)
    refused = []

    def probe(db, table, missing):
        other = sqlite3.connect(p, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            refused.append(str(e))
        else:
            other.rollback()
        finally:
            other.close()

    monkeypatch.setattr(sqlitedb, "_before_add_columns", probe)
    open_db(p, SCHEMA).close()
    assert refused and "locked" in refused[0]


def test_a_column_someone_else_already_added_is_harmless(tmp_path, monkeypatch):
    """Safeguard 2: 'duplicate column name' from ALTER is not an error."""
    p = tmp_path / "x.db"
    open_db(p, SCHEMA).close()  # columns present
    # ...but this opener's look at the table predates them (as for a racer
    # that read table_info before another process's ALTER committed).
    monkeypatch.setattr(sqlitedb, "_existing_columns", lambda db, table: {"id", "a"})
    open_db(p, SCHEMA).close()
    assert _cols(p) == ["id", "a", "b", "c"]


def test_other_alter_errors_still_raise(tmp_path):
    bad = Schema("bad", statements=SCHEMA.statements, columns={"t": {"b": "NOT A TYPE ((("}})
    with pytest.raises(sqlite3.OperationalError):
        open_db(tmp_path / "x.db", bad)


WAL = Schema("walled", statements=SCHEMA.statements, wal=True)


def test_wal_switch_is_retried_while_locked(tmp_path, monkeypatch):
    """Safeguard 3: SQLite can answer the WAL switch with SQLITE_BUSY at once."""
    real = sqlitedb._set_wal
    calls = []

    def flaky(db):
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        real(db)

    monkeypatch.setattr(sqlitedb, "_set_wal", flaky)
    db = open_db(tmp_path / "x.db", WAL)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.close()
    assert len(calls) == 3


def test_wal_retry_gives_up_at_the_busy_timeout(tmp_path, monkeypatch):
    def locked(db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlitedb, "_set_wal", locked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        open_db(tmp_path / "x.db", WAL, busy_timeout_s=0.2)


def test_wal_switch_does_not_retry_other_errors(tmp_path, monkeypatch):
    calls = []

    def broken(db):
        calls.append(1)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlitedb, "_set_wal", broken)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        open_db(tmp_path / "x.db", WAL)
    assert calls == [1]


def test_non_wal_schemas_keep_the_rollback_journal(tmp_path):
    db = open_db(tmp_path / "x.db", SCHEMA)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()


def test_row_factory(tmp_path):
    db = open_db(tmp_path / "x.db", SCHEMA, row_factory=sqlite3.Row)
    assert db.row_factory is sqlite3.Row
    db.close()


# --- the race, forced --------------------------------------------------------------


class ForcedRace:
    """Hooks that put two openers into the check-then-ALTER window together.

    Opener A, holding whatever lock the helper took, waits at the migration
    point until opener B has reached the lock acquisition, then gives B a
    grace period to join it there. If the helper serializes schema work, B is
    blocked on the lock, the grace period runs out, and A finishes first. If it
    does not, B joins A and both ALTER the same columns: the race, every time.
    """

    def __init__(self, n=2, grace_s=1.0):
        self.reached_lock = threading.Barrier(n)
        self.at_migration = threading.Barrier(n)
        self.grace_s = grace_s

    def before_lock(self, db):
        try:
            self.reached_lock.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass

    def before_add_columns(self, db, table, missing):
        try:
            self.at_migration.wait(timeout=self.grace_s)
        except threading.BrokenBarrierError:
            pass

    def install(self, monkeypatch):
        monkeypatch.setattr(sqlitedb, "_before_lock", self.before_lock)
        monkeypatch.setattr(sqlitedb, "_before_add_columns", self.before_add_columns)


def run_together(fn, n=2):
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 - asserted on below
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return errors


def test_two_openers_forced_into_the_migration_window(tmp_path, monkeypatch):
    p = tmp_path / "x.db"
    _old(p)
    ForcedRace().install(monkeypatch)
    errors = run_together(lambda: open_db(p, SCHEMA).close())
    assert errors == []
    assert _cols(p) == ["id", "a", "b", "c"]
