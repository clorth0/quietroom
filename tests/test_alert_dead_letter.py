"""Alert outbox dead-letter: one bad message must not block its channel forever."""

import json
import sqlite3


from quietroom.alerts import Outbox, QueuedChannel, outbox_rows, requeue_parked


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def ob(tmp_path, clock, **kw):
    kw.setdefault("backoff_s", 30)
    kw.setdefault("max_backoff_s", 3600)
    kw.setdefault("max_attempts", 3)
    return Outbox(tmp_path / "o.db", channel="email", clock=clock, **kw)


def poison_send(sent, bad=1):
    def send(p):
        if p["n"] == bad:
            raise ValueError("550 5.6.0 message rejected")
        sent.append(p["n"])

    return send


def run(o, send, clock, until, step=10.0):
    while clock.t < until:
        clock.t += step
        o.flush(send)


def test_poison_message_blocks_only_for_its_attempt_budget_then_is_parked(tmp_path):
    clock, sent = Clock(), []
    o = ob(tmp_path, clock)
    o.add({"n": 1})
    o.add({"n": 2})
    send = poison_send(sent)
    o.flush(send)  # attempt 1 (fails, backs off 30 s)
    run(o, send, clock, 1000 + 30 + 60 - 1)
    assert sent == []  # within the budget the head still holds the channel (FIFO)
    run(o, send, clock, 1000 + 30 + 60 + 10)  # attempt 3 fails -> suspect; 2 goes out
    assert sent == [2]
    assert o.pending() == 1 and o.parked() == 0  # not parked yet: never failed after a success
    run(o, send, clock, 1000 + 30 + 60 + 120 + 20)  # its next probe fails while channel works
    assert (o.pending(), o.parked()) == (0, 1)
    [row] = outbox_rows(tmp_path / "o.db", parked=True)
    assert row["park_reason"].startswith("poison") and "550" in row["last_error"]
    assert row["attempts"] == 4 and row["channel"] == "email" and row["payload"] == {"n": 1}
    o.add({"n": 3})
    clock.t += 1
    assert o.flush(send) == 1 and sent == [2, 3]  # parked rows are never retried


def test_an_outage_never_parks_and_costs_one_attempt_per_flush(tmp_path):
    clock, calls = Clock(), []
    o = ob(tmp_path, clock, max_backoff_s=300)

    def down(p):
        calls.append(p["n"])
        raise OSError("connection refused")

    for n in (1, 2, 3):
        o.add({"n": n})
    flushes = 0
    while clock.t < 1000 + 6 * 3600:  # six hours down
        clock.t += 5
        before = len(calls)
        o.flush(down)
        flushes += 1
        assert len(calls) - before <= 1  # a dead relay costs one timeout, not one per alert
    assert o.parked() == 0 and o.pending() == 3
    assert {r["attempts"] >= 3 for r in outbox_rows(tmp_path / "o.db")} == {True}
    clock.t += 3600
    sent = []
    assert o.flush(lambda p: sent.append(p["n"])) == 3 and sent == [1, 2, 3]  # all, in order
    assert o.parked() == 0


def test_a_poison_head_does_not_starve_messages_exhausted_by_an_outage(tmp_path):
    clock, sent = Clock(), []
    o = ob(tmp_path, clock, max_backoff_s=600)

    def down(p):
        raise OSError("connection refused")

    for n in (1, 2, 3):
        o.add({"n": n})
    run(o, down, clock, 1000 + 3 * 3600, step=5)  # outage exhausts all three
    send = poison_send(sent, bad=1)  # relay back; message 1 is genuinely bad
    run(o, send, clock, clock.t + 3 * 600, step=5)
    assert sent == [2, 3] and o.parked() == 1
    assert outbox_rows(tmp_path / "o.db", parked=True)[0]["payload"] == {"n": 1}


def test_max_age_parks_even_while_the_channel_is_down(tmp_path):
    clock = Clock()
    o = ob(tmp_path, clock, max_attempts=0, max_age_s=3600)  # attempts check disabled

    def down(p):
        raise OSError("connection refused")

    o.add({"n": 1})
    run(o, down, clock, 1000 + 3570, step=30)
    assert clock.t == 1000 + 3570
    assert o.parked() == 0
    clock.t = 1000 + 3600
    o.flush(down)
    assert (o.pending(), o.parked()) == (0, 1)
    assert outbox_rows(tmp_path / "o.db", parked=True)[0]["park_reason"].startswith("max_age")


def test_disabled_limits_keep_the_old_behaviour(tmp_path):
    clock, sent = Clock(), []
    o = ob(tmp_path, clock, max_attempts=0, max_age_s=0)
    o.add({"n": 1})
    o.add({"n": 2})
    run(o, poison_send(sent), clock, 1000 + 30 * 86400, step=3600)
    assert sent == [] and o.parked() == 0 and o.pending() == 2


def test_requeue_restores_attempts_age_and_fifo_position(tmp_path):
    clock, sent = Clock(), []
    o = ob(tmp_path, clock, max_age_s=100)
    o.add({"n": 1})
    clock.t += 200
    o.flush(poison_send(sent, bad=99))  # too old: parked before any attempt
    assert o.parked() == 1 and sent == []
    o.add({"n": 2})
    [row] = outbox_rows(tmp_path / "o.db", parked=True)
    assert requeue_parked(tmp_path / "o.db", ids=[row["id"]], now=clock.t) == [row["id"]]
    assert o.parked() == 0 and o.pending() == 2
    clock.t += 1
    assert o.flush(poison_send(sent, bad=99)) == 2 and sent == [1, 2]  # original order


def test_requeue_all_by_channel_and_unknown_ids(tmp_path):
    clock = Clock()
    path = tmp_path / "o.db"
    email = Outbox(path, channel="email", clock=clock, max_age_s=10)
    hook = Outbox(path, channel="webhook", clock=clock, max_age_s=10)
    email.add({"n": 1})
    hook.add({"n": 2})
    clock.t += 20

    def down(p):
        raise OSError("down")

    email.flush(down)
    hook.flush(down)
    assert (email.parked(), hook.parked()) == (1, 1)
    assert requeue_parked(path, channel="webhook", now=clock.t) == [2]
    assert (email.parked(), hook.parked()) == (1, 0)
    assert requeue_parked(path, ids=[2, 404], now=clock.t) == []  # not parked / no such row
    assert requeue_parked(path, now=clock.t) == [1]


def test_migration_from_the_0_3_schema_does_not_age_out_old_rows_instantly(tmp_path):
    p = tmp_path / "o.db"
    db = sqlite3.connect(p)
    db.execute(
        "CREATE TABLE outbox(id INTEGER PRIMARY KEY, payload TEXT, attempts INTEGER DEFAULT 0,"
        " channel TEXT DEFAULT 'default', next_attempt_at REAL DEFAULT 0,"
        " last_error TEXT DEFAULT '')"
    )
    db.execute("INSERT INTO outbox(payload,channel,attempts) VALUES('{\"n\": 1}','email',7)")
    db.commit()
    db.close()
    clock = Clock(5000.0)
    o = Outbox(p, channel="email", clock=clock, max_attempts=3, max_age_s=100)
    clock.t += 50

    def down(e):
        raise OSError("down")

    o.flush(down)
    assert o.parked() == 0  # age counts from the upgrade, and an outage never parks
    clock.t += 60
    o.flush(down)
    assert o.parked() == 1


def test_queued_channel_reports_parked_and_what_it_parked(tmp_path):
    clock, sent = Clock(), []
    o = ob(tmp_path, clock, max_age_s=10)
    ch = QueuedChannel("email", poison_send(sent), o)
    ch({"n": 5})
    clock.t += 20
    ch.flush()
    assert ch.parked() == 1 and ch.pending() == 0
    [p] = ch.last_parked
    assert p["reason"].startswith("max_age") and p["id"] == 1 and "payload" not in p


def test_outbox_rows_summarise_alert_payloads(tmp_path):
    clock = Clock()
    o = ob(tmp_path, clock)
    o.add({"segment": "uhf", "freq_hz": 308e6, "score": 91.0, "node_id": "n1"})
    [row] = outbox_rows(tmp_path / "o.db")
    assert row["summary"] == "n1 uhf 308.000 MHz score 91"
    assert json.dumps(row)  # JSON-serialisable for the CLI
