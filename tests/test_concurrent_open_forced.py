"""The concurrent-open race, forced deterministically for every database in the repo.

The barrier hooks in ``quietroom.sqlitedb`` put two openers into the
check-then-ALTER window at the same moment (see ``ForcedRace``). With the
helper's safeguards in place both succeed; with them removed the second ALTER
fails with ``duplicate column name`` on every run, not one run in three.
"""

from __future__ import annotations

import sqlite3

import pytest

from quietroom.alerts import Outbox, outbox_rows
from quietroom.fleet import FleetClient, fleet_outbox_rows
from quietroom.store import Store

from test_sqlitedb import ForcedRace, run_together

OLD_STORE = """
CREATE TABLE nodes(node_id TEXT PRIMARY KEY, site TEXT, floor TEXT, room TEXT, updated_at REAL);
CREATE TABLE baselines(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, version INTEGER,
 created_at REAL, sweep_count INTEGER, blob BLOB, UNIQUE(node_id,segment,version));
INSERT INTO baselines(node_id,segment,version,created_at,sweep_count,blob)
 VALUES('n1','uhf',1,0,3,x'00');
"""
OLD_ALERTS = """
CREATE TABLE outbox(id INTEGER PRIMARY KEY, payload TEXT, attempts INTEGER DEFAULT 0);
INSERT INTO outbox(payload) VALUES('{"score": 90}');
"""
OLD_FLEET = """
CREATE TABLE fleet_outbox(id INTEGER PRIMARY KEY, path TEXT, payload TEXT);
INSERT INTO fleet_outbox(path,payload) VALUES('/api/v1/events','{}');
"""


def _store(p):
    Store(p).close()


def _alerts(p):
    Outbox(p, channel="webhook").db.close()


def _alerts_cli(p):
    outbox_rows(p)


def _fleet(p):
    FleetClient("https://central.invalid", outbox_path=p).db.close()


def _fleet_cli(p):
    fleet_outbox_rows(p)


CASES = {
    "store": (OLD_STORE, _store, "baselines", {"reason", "fingerprint", "captured_at", "detail"}),
    "alert-outbox": (OLD_ALERTS, _alerts, "outbox", {"channel", "created_at", "parked_at"}),
    "alert-outbox-cli": (OLD_ALERTS, _alerts_cli, "outbox", {"channel", "created_at"}),
    "fleet-outbox": (OLD_FLEET, _fleet, "fleet_outbox", {"created_at", "attempts", "parked_at"}),
    "fleet-outbox-cli": (OLD_FLEET, _fleet_cli, "fleet_outbox", {"created_at", "parked_at"}),
}


@pytest.mark.parametrize("case", list(CASES))
def test_two_openers_of_an_old_file_forced_into_the_migration_window(tmp_path, monkeypatch, case):
    old, opener, table, added = CASES[case]
    p = tmp_path / "x.db"
    db = sqlite3.connect(p)
    db.executescript(old)
    db.close()
    ForcedRace().install(monkeypatch)
    errors = run_together(lambda: opener(p))
    assert errors == [], errors
    db = sqlite3.connect(p)
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    rows = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    db.close()
    assert len(cols) == len(set(cols))
    assert added <= set(cols)
    assert rows == 1  # the pre-existing row survived
