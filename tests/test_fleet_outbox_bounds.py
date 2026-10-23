"""The node's fleet outbox is bounded and sets aside what it cannot deliver.

Mirrors the alert dead-letter design (#54): an outage parks nothing on its own,
a payload that keeps failing while central accepts others is parked as poison,
anything undelivered past max_age_s is parked as stale, and parked rows are
kept, listed and requeued with ``quietroom-node --outbox ... --channel fleet``.
Unlike alerts the queue also has a hard row bound: past ``outbox_max_rows`` the
oldest pending rows are parked, and past the same bound of parked rows the
oldest parked are dropped and counted, so the file cannot grow forever.

  AC13 heartbeats coalesce: only the newest queued heartbeat is kept
  AC14 row bound: pending <= max_rows, overflow parked oldest-first, parked capped and counted
  AC15 an outage parks nothing (except by age/bound) and costs one attempt per flush
  AC16 poison: parked only after failing again once central accepted later rows
  AC17 stale rows are parked by age
  AC18 old outbox files upgrade in place; parked rows can be listed and requeued
  AC19 health and heartbeat report pending / parked / dropped
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from quietroom.fleet import FleetClient, fleet_outbox_rows, requeue_fleet_parked


class Central:
    """Fake central: up/down, and can reject payloads containing a marker."""

    def __init__(self):
        self.up = True
        self.reject = ""
        self.got: list[tuple[str, dict]] = []
        self.calls = 0

    def __call__(self, url, body, headers):
        self.calls += 1
        if not self.up:
            raise ConnectionError("central down")
        if self.reject and self.reject.encode() in body:
            raise RuntimeError("HTTP 400 bad payload")
        self.got.append((url.rsplit("/", 1)[-1], json.loads(body)))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def client(tmp_path, central, **kw):
    kw.setdefault("clock", Clock())
    return FleetClient(
        "https://c.test", token="t", sender=central, outbox_path=tmp_path / "f.db", **kw
    )


def ev(cl, i, **extra):
    return cl.send_events("n1", [{"i": i, **extra}])


def sent_ids(central):
    return [p["events"][0]["i"] for kind, p in central.got if kind == "events"]


# --- AC13 -----------------------------------------------------------------------------


def test_queued_heartbeats_coalesce_to_the_newest(tmp_path):
    c = Central()
    c.up = False
    cl = client(tmp_path, c)
    for i in range(50):
        cl.send_heartbeat({"node_id": "n1", "seq": i})
    ev(cl, 1)
    assert cl.pending() == 2  # one heartbeat + one event, not 51 rows
    c.up = True
    assert cl.flush() == 2
    beats = [p for kind, p in c.got if kind == "heartbeat"]
    assert [b["seq"] for b in beats] == [49]


# --- AC14 -----------------------------------------------------------------------------


def test_pending_rows_are_bounded_and_overflow_is_parked_oldest_first(tmp_path):
    c = Central()
    c.up = False
    cl = client(tmp_path, c, max_rows=5)
    for i in range(8):
        ev(cl, i)
    assert (cl.pending(), cl.parked(), cl.dropped()) == (5, 3, 0)
    parked = fleet_outbox_rows(tmp_path / "f.db", parked=True)
    assert [r["payload"]["events"][0]["i"] for r in parked] == [0, 1, 2]
    assert all("overflow" in r["park_reason"] and "5" in r["park_reason"] for r in parked)
    c.up = True
    cl.flush()
    assert sent_ids(c) == [3, 4, 5, 6, 7]  # newest data first after an overflow, in order


def test_parked_rows_are_capped_too_and_drops_are_counted(tmp_path):
    c = Central()
    c.up = False
    cl = client(tmp_path, c, max_rows=3)
    for i in range(10):
        ev(cl, i)
    assert (cl.pending(), cl.parked(), cl.dropped()) == (3, 3, 4)
    db = sqlite3.connect(tmp_path / "f.db")
    assert db.execute("SELECT COUNT(*) FROM fleet_outbox").fetchone()[0] == 6  # 2 x max_rows
    # the count survives a restart
    assert client(tmp_path, c, max_rows=3).dropped() == 4


# --- AC15 -----------------------------------------------------------------------------


def test_an_outage_parks_nothing_and_costs_one_attempt_per_flush(tmp_path):
    c = Central()
    cl = client(tmp_path, c, max_attempts=2)
    c.up = False
    for i in range(4):
        ev(cl, i)
    c.calls = 0
    for _ in range(30):
        assert cl.flush() == 0
    assert c.calls == 30
    assert (cl.pending(), cl.parked()) == (4, 0)
    c.up = True
    assert cl.flush() == 4
    assert sent_ids(c) == [0, 1, 2, 3]


# --- AC16 -----------------------------------------------------------------------------


def test_a_poison_payload_is_parked_once_central_accepts_later_rows(tmp_path):
    c = Central()
    c.reject = "POISON"
    cl = client(tmp_path, c, max_attempts=3)
    c.up = False  # queue behind an outage so the poison is at the head
    ev(cl, 0, tag="POISON")
    ev(cl, 1)
    ev(cl, 2)
    c.up = True
    for _ in range(2):  # attempts 1 and 2: still holds the queue (could be central)
        assert cl.flush() == 0
    assert sent_ids(c) == [] and cl.parked() == 0
    assert cl.flush() == 0  # attempt 3: now a suspect
    assert cl.flush() == 2  # suspect skipped, later rows delivered, suspect retried and parked
    assert sent_ids(c) == [1, 2]
    assert (cl.pending(), cl.parked()) == (0, 1)
    [row] = fleet_outbox_rows(tmp_path / "f.db", parked=True)
    assert "poison" in row["park_reason"] and row["attempts"] == 4
    assert "HTTP 400" in row["last_error"]
    assert cl.last_parked and cl.last_parked[0]["reason"].startswith("poison")


def test_max_attempts_zero_never_parks_poison(tmp_path):
    c = Central()
    c.reject = "POISON"
    cl = client(tmp_path, c, max_attempts=0)
    ev(cl, 0, tag="POISON")
    ev(cl, 1)
    for _ in range(20):
        cl.flush()
    assert (cl.pending(), cl.parked()) == (2, 0)  # head-of-line, as before this change


# --- AC17 -----------------------------------------------------------------------------


def test_rows_older_than_max_age_are_parked_as_stale(tmp_path):
    c = Central()
    c.up = False
    clock = Clock()
    cl = client(tmp_path, c, clock=clock, max_age_s=3600)
    ev(cl, 0)
    clock.t += 1800
    ev(cl, 1)
    clock.t += 1801
    cl.flush()
    assert (cl.pending(), cl.parked()) == (1, 1)
    [row] = fleet_outbox_rows(tmp_path / "f.db", parked=True)
    assert "stale" in row["park_reason"]


# --- AC18 -----------------------------------------------------------------------------


def test_an_old_outbox_file_upgrades_in_place(tmp_path):
    p = tmp_path / "f.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE fleet_outbox(id INTEGER PRIMARY KEY, path TEXT, payload TEXT)")
    db.execute(
        "INSERT INTO fleet_outbox(path,payload) VALUES('/api/v1/events', ?)",
        (json.dumps({"node_id": "n1", "events": [{"i": 9}]}),),
    )
    db.commit()
    db.close()
    c = Central()
    cl = client(tmp_path, c)
    assert cl.pending() == 1
    assert cl.flush() == 1 and sent_ids(c) == [9]


def test_parked_rows_can_be_requeued(tmp_path):
    c = Central()
    c.up = False
    cl = client(tmp_path, c, max_rows=2)
    for i in range(4):
        ev(cl, i)
    ids = [r["id"] for r in fleet_outbox_rows(tmp_path / "f.db", parked=True)]
    assert requeue_fleet_parked(tmp_path / "f.db", ids=ids[:1]) == ids[:1]
    assert requeue_fleet_parked(tmp_path / "f.db") == ids[1:]
    c.up = True
    cl.flush()
    assert sorted(sent_ids(c)) == [0, 1, 2, 3]


def test_cli_lists_and_requeues_the_fleet_outbox(tmp_path, capsys):
    from quietroom.node import cli

    conf = tmp_path / "node.toml"
    conf.write_text(
        f'[node]\nid = "n1"\n[store]\npath = "{tmp_path / "q.db"}"\n'
        '[[segments]]\nname = "uhf"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n'
        '[fleet]\nbase_url = "https://c.test"\nauth = "legacy-token"\noutbox_max_rows = 1\n'
    )
    outbox = cli._fleet_outbox(cli.load_config(conf))
    c = Central()
    c.up = False
    cl = FleetClient("https://c.test", token="t", sender=c, outbox_path=outbox, max_rows=1)
    ev(cl, 0)
    ev(cl, 1)
    cl.db.close()
    assert cli.main(["--config", str(conf), "--outbox", "list", "--channel", "fleet"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["state"] for r in rows] == ["parked", "pending"]
    assert all("payload" in r and r["channel"] == "fleet" for r in rows)
    pid = rows[0]["id"]
    rc = cli.main(
        ["--config", str(conf), "--outbox", "requeue", "--channel", "fleet", "--id", str(pid)]
    )
    assert rc == 0 and json.loads(capsys.readouterr().out) == {"requeued": [pid]}


# --- AC19 -----------------------------------------------------------------------------


def test_health_and_heartbeat_report_the_outbox(tmp_path):
    from quietroom.config import default_config
    from quietroom.fleet import heartbeat_payload, NodeIdentity
    from quietroom.node.service import MonitorService

    c = Central()
    c.up = False
    cl = client(tmp_path, c, max_rows=2)
    for i in range(5):
        ev(cl, i)
    svc = MonitorService(default_config(), None, fleet=cl)
    h = svc.health()
    assert h["fleet_outbox"] == {"pending": 2, "parked": 2, "dropped": 1}
    assert h["fleet_pending"] == 2
    ident = NodeIdentity.load_or_create(tmp_path / "id.json")
    assert heartbeat_payload(ident, h)["fleet_outbox"] == h["fleet_outbox"]


@pytest.mark.parametrize("kw", [{"max_rows": 0}, {"max_rows": -1}, {"max_attempts": -1},
                                {"max_age_s": -1}, {"flush_batch": 0}])
def test_the_client_refuses_nonsense_limits(tmp_path, kw):
    with pytest.raises(ValueError):
        client(tmp_path, Central(), **kw)


def test_the_service_reports_what_the_fleet_outbox_set_aside(tmp_path, capsys):
    from quietroom.config import default_config
    from quietroom.node.service import MonitorService

    c = Central()
    c.up = False
    cl = client(tmp_path, c, max_rows=1)
    events = []
    svc = MonitorService(default_config(), None, sink=events.append, fleet=cl)
    ev(cl, 0)
    ev(cl, 1)  # overflow parks row 0
    svc._report_fleet_parked()
    [e] = [e for e in events if e["type"] == "fleet_parked"]
    assert "overflow" in e["parked"][0]["reason"] and "--channel fleet" in e["hint"]
    assert "fleet outbox set aside 1" in capsys.readouterr().err
    svc._report_fleet_parked()  # reported once, not every tick
    assert len([e for e in events if e["type"] == "fleet_parked"]) == 1


def test_rows_parked_while_enqueuing_are_still_reported_after_a_flush(tmp_path, capsys):
    # Review F1: flush() must not discard what an earlier enqueue set aside.
    from quietroom.config import default_config
    from quietroom.node.service import MonitorService

    c = Central()
    c.up = False
    cl = client(tmp_path, c, max_rows=1)
    events = []
    svc = MonitorService(default_config(), None, sink=events.append, fleet=cl)
    ev(cl, 0)
    ev(cl, 1)  # overflow parks row 0 during the enqueue
    cl.flush()  # still down: parks nothing itself
    svc._report_fleet_parked()
    [e] = [e for e in events if e["type"] == "fleet_parked"]
    assert "overflow" in e["parked"][0]["reason"]


def test_central_going_down_mid_flush_parks_no_suspect(tmp_path):
    # Review F2: a success followed by a failure in one flush is an outage starting,
    # not proof that the suspects are poison.
    c = Central()
    cl = client(tmp_path, c, max_attempts=1)
    c.up = False
    ev(cl, 0)
    cl.flush()  # row 0: one failure -> suspect
    ev(cl, 1)
    ev(cl, 2)

    calls = []

    def flaky(url, body, headers):
        calls.append(body)
        if len(calls) > 1:
            raise ConnectionError("central went down")
        c.got.append(("events", json.loads(body)))

    cl.sender = flaky
    assert cl.flush() == 1  # row 1 delivered, row 2 failed: central is down now
    assert cl.parked() == 0 and cl.pending() == 2


def test_one_flush_delivers_a_bounded_batch(tmp_path):
    # Review F5: draining a 20k backlog in one tick would starve the watchdog.
    c = Central()
    c.up = False
    cl = client(tmp_path, c, flush_batch=50)
    for i in range(120):
        ev(cl, i)
    c.up = True
    assert [cl.flush(), cl.flush(), cl.flush()] == [50, 50, 20]
    assert sent_ids(c) == list(range(120))


# Concurrent upgrade of an old fleet outbox: tests/test_concurrent_open_forced.py
# (deterministic; replaces the thread-pile test that caught the race 3 runs in 10).
