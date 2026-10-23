"""The alert outbox sends at most flush_batch rows per flush (per tick), like the fleet's."""

from __future__ import annotations

import pytest

from quietroom.alerts import ALERT_FLUSH_BATCH, Outbox, QueuedChannel
from quietroom.fleet import FLUSH_BATCH


def _box(tmp_path, n, **kw):
    box = Outbox(tmp_path / "o.db", channel="webhook", **kw)
    for i in range(n):
        box.add({"i": i})
    return box


def test_default_matches_the_fleet_outbox():
    assert ALERT_FLUSH_BATCH == FLUSH_BATCH == 500


def test_a_backlog_drains_in_batches_in_order(tmp_path):
    box = _box(tmp_path, 12, flush_batch=5)
    sent = []
    assert [box.flush(sent.append) for _ in range(4)] == [5, 5, 2, 0]
    assert [p["i"] for p in sent] == list(range(12))


def test_failures_count_against_the_batch(tmp_path):
    box = _box(tmp_path, 12, flush_batch=5)
    calls = []

    def fail(p):
        calls.append(p)
        raise OSError("down")

    box.flush(fail, stop_on_failure=False)
    assert len(calls) == 5  # bounded work per tick even when nothing gets through


def test_suspects_count_against_the_batch(tmp_path):
    box = _box(tmp_path, 8, flush_batch=3, max_attempts=1)
    box.db.execute("UPDATE outbox SET attempts=1")  # all suspects
    box.db.commit()
    calls = []
    box.flush(calls.append)
    assert len(calls) == 3


def test_default_batch_caps_a_large_backlog(tmp_path):
    box = _box(tmp_path, ALERT_FLUSH_BATCH + 20)
    ch = QueuedChannel("webhook", lambda p: None, box)
    assert ch.flush() == ALERT_FLUSH_BATCH
    assert ch.pending() == 20


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5"])
def test_nonsense_batch_sizes_are_refused(tmp_path, bad):
    with pytest.raises(ValueError, match="flush_batch"):
        Outbox(tmp_path / "o.db", flush_batch=bad)
