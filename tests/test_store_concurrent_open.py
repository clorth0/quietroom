"""Store() must be safe to open from several connections at once.

The node daemon and ``quietroom-node --rebaseline/--ack/--promote-anchor`` open
the same SQLite file concurrently. Opening runs one-time setup (switch to WAL,
create tables, add columns to an older schema), and two openers racing through
that setup used to fail with ``database is locked`` or ``duplicate column name``.
"""

import multiprocessing as mp
import sqlite3
import threading

import pytest

from quietroom.store import _BASELINE_COLUMNS, Store

OPENERS = 16
ROUNDS = 8

# The baselines table as it shipped before the re-baseline policy added columns.
OLD_SCHEMA = """
CREATE TABLE nodes(node_id TEXT PRIMARY KEY, site TEXT, floor TEXT, room TEXT, updated_at REAL);
CREATE TABLE baselines(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, version INTEGER,
 created_at REAL, sweep_count INTEGER, blob BLOB, UNIQUE(node_id,segment,version));
INSERT INTO baselines(node_id,segment,version,created_at,sweep_count,blob)
 VALUES('n1','uhf',1,0,3,x'00');
"""


def _open_all_at_once(path, n=OPENERS):
    """Open n Stores on n threads released together; return the errors raised."""
    gate = threading.Barrier(n)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def opener():
        gate.wait()
        try:
            Store(path).close()
        except BaseException as e:  # noqa: BLE001 - collected and asserted on below
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=opener) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def _columns(path):
    db = sqlite3.connect(path)
    try:
        return [r[1] for r in db.execute("PRAGMA table_info(baselines)")]
    finally:
        db.close()


def _journal_mode(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        db.close()


def test_many_openers_of_a_brand_new_store_all_succeed(tmp_path):
    for i in range(ROUNDS):
        path = str(tmp_path / f"fresh{i}.db")
        errors = _open_all_at_once(path)
        assert errors == [], f"round {i}: {errors[:3]}"
        assert _journal_mode(path) == "wal"
        assert set(_BASELINE_COLUMNS) <= set(_columns(path))


def test_many_openers_of_an_old_store_migrate_it_exactly_once(tmp_path):
    for i in range(ROUNDS):
        path = str(tmp_path / f"old{i}.db")
        db = sqlite3.connect(path)
        db.executescript(OLD_SCHEMA)
        db.close()
        errors = _open_all_at_once(path)
        assert errors == [], f"round {i}: {errors[:3]}"
        cols = _columns(path)
        assert len(cols) == len(set(cols))
        assert set(_BASELINE_COLUMNS) <= set(cols)
        s = Store(path)
        try:  # the pre-existing row survived the migration
            assert s.db.execute("SELECT count(*) FROM baselines").fetchone()[0] == 1
        finally:
            s.close()


def _proc_open(path, gate, out):
    gate.wait()
    try:
        Store(path).close()
        out.put("ok")
    except Exception as e:  # noqa: BLE001 - reported to the parent
        out.put(f"{type(e).__name__}: {e}")


@pytest.mark.parametrize("old", [False, True], ids=["new", "old-schema"])
def test_separate_processes_opening_at_once_all_succeed(tmp_path, old):
    """Same race across processes, as with the daemon and the CLI."""
    path = str(tmp_path / "proc.db")
    if old:
        db = sqlite3.connect(path)
        db.executescript(OLD_SCHEMA)
        db.close()
    ctx = mp.get_context("spawn")
    n = 6
    gate, out = ctx.Barrier(n), ctx.Queue()
    procs = [ctx.Process(target=_proc_open, args=(path, gate, out)) for _ in range(n)]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert results == ["ok"] * n
    assert set(_BASELINE_COLUMNS) <= set(_columns(path))
