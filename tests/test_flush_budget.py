"""Each outbox flush has a wall-clock budget as well as the 500-send cap.

A relay that is up but slow (say 1 s per SMTP send) would otherwise hold a
tick for up to 500 s, far past the systemd watchdog (WatchdogSec=120), and
the final shutdown flush past TimeoutStopSec=60 (then SIGKILL). Once the
budget is spent no new send starts; the send in flight finishes, and the
rest stay queued, in order, for the next flush.

Uses a fake slow sink with a real delay, so the timings are real.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from quietroom.alerts import ALERT_FLUSH_BUDGET_S, Outbox, QueuedChannel
from quietroom.config import ConfigError, NodeConfig, config_warnings
from quietroom.fleet import FLUSH_BUDGET_S, FleetClient
from quietroom.node.service import SHUTDOWN_FLUSH_BUDGET_S, MonitorService

N = 20
DELAY = 0.05  # per send; no budget: a flush of N takes about N * DELAY = 1.0 s
BUDGET = 0.3
SLACK = 0.25  # scheduler noise on a loaded CI box


class SlowSink:
    def __init__(self, delay=DELAY):
        self.delay = delay
        self.got: list = []

    def __call__(self, *payload):
        time.sleep(self.delay)
        self.got.append(payload[-1] if len(payload) == 1 else payload[1])


def _alert_outbox(tmp_path, **kw) -> Outbox:
    ob = Outbox(tmp_path / "alerts", channel="email", **kw)
    for i in range(N):
        ob.add({"i": i})
    return ob


def _timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


# --- alert outbox -----------------------------------------------------------------


def test_the_default_budget_is_well_inside_the_watchdog():
    # 2 alert channels + fleet, each a full budget plus one 10 s send in flight,
    # must fit WatchdogSec=120 with room for the sweep itself.
    assert ALERT_FLUSH_BUDGET_S == FLUSH_BUDGET_S == 15.0
    assert 2 * (ALERT_FLUSH_BUDGET_S + 10) + (FLUSH_BUDGET_S + 10) <= 90
    # shutdown: the budget plus one send in flight fits TimeoutStopSec=60
    assert SHUTDOWN_FLUSH_BUDGET_S == 30.0
    assert SHUTDOWN_FLUSH_BUDGET_S + 10 < 60


def test_alert_flush_without_a_budget_takes_n_times_the_delay(tmp_path):
    ob = _alert_outbox(tmp_path, flush_budget_s=60)
    sink = SlowSink()
    sent, took = _timed(lambda: ob.flush(sink))
    assert sent == N and ob.pending() == 0
    assert took >= N * DELAY


def test_alert_flush_stops_near_the_budget_and_keeps_the_rest_queued(tmp_path):
    ob = _alert_outbox(tmp_path, flush_budget_s=BUDGET)
    sink = SlowSink()
    sent, took = _timed(lambda: ob.flush(sink))
    assert BUDGET <= took <= BUDGET + DELAY + SLACK, took
    assert 0 < sent < N
    assert ob.pending() == N - sent  # nothing lost, nothing parked
    assert ob.parked() == 0
    assert [p["i"] for p in sink.got] == list(range(sent))  # oldest first
    assert ob.last_flush_budget_spent is True
    # the next flush carries on in order
    ob.flush_budget_s = 60
    ob.flush(sink)
    assert [p["i"] for p in sink.got] == list(range(N))
    assert ob.last_flush_budget_spent is False


def test_a_per_call_budget_overrides_the_default(tmp_path):
    ob = _alert_outbox(tmp_path, flush_budget_s=60)
    sent, took = _timed(lambda: ob.flush(SlowSink(), budget_s=BUDGET))
    assert took <= BUDGET + DELAY + SLACK and sent < N


def test_a_spent_budget_starts_no_send(tmp_path):
    ob = _alert_outbox(tmp_path)
    sink = SlowSink()
    assert ob.flush(sink, budget_s=0) == 0
    assert sink.got == [] and ob.pending() == N


def test_alert_flush_stops_starting_sends_once_a_stop_is_requested(tmp_path):
    ob = _alert_outbox(tmp_path, flush_budget_s=60)
    sink = SlowSink()
    ob.flush(sink, should_stop=lambda: len(sink.got) >= 3)
    assert len(sink.got) == 3 and ob.pending() == N - 3


def test_the_budget_also_bounds_the_suspect_pass(tmp_path):
    ob = Outbox(tmp_path / "alerts", channel="email", max_attempts=1, flush_budget_s=60)
    for i in range(N):
        ob.add({"i": i})
    ob.db.execute("UPDATE outbox SET attempts=1")  # every row is a suspect
    ob.db.commit()
    sent, took = _timed(lambda: ob.flush(SlowSink(), budget_s=BUDGET))
    assert took <= BUDGET + DELAY + SLACK and 0 < sent < N


def test_queued_channel_passes_the_budget_through(tmp_path):
    ob = _alert_outbox(tmp_path, flush_budget_s=60)
    ch = QueuedChannel("email", SlowSink(), ob)
    _, took = _timed(lambda: ch.flush(budget_s=BUDGET))
    assert took <= BUDGET + DELAY + SLACK and ob.pending() > 0


@pytest.mark.parametrize("bad", [0, -1, True, "15", math.nan, math.inf, None])
def test_alert_flush_budget_is_validated(tmp_path, bad):
    with pytest.raises(ValueError, match="flush_budget_s"):
        Outbox(tmp_path / "x", flush_budget_s=bad)


# --- fleet outbox -----------------------------------------------------------------


def _fleet(tmp_path, sink, **kw) -> FleetClient:
    down = FleetClient("http://central", sender=_fail, outbox_path=tmp_path / "fleet", **kw)
    for i in range(N):
        down.send_events("n", [{"i": i}])  # central down: everything queues
    assert down.pending() == N
    down.sender = sink
    return down


def _fail(*a):
    raise OSError("central down")


def test_fleet_flush_without_a_budget_takes_n_times_the_delay(tmp_path):
    fc = _fleet(tmp_path, SlowSink(), flush_budget_s=60)
    sent, took = _timed(fc.flush)
    assert sent == N and fc.pending() == 0 and took >= N * DELAY


def test_fleet_flush_stops_near_the_budget_and_keeps_the_rest_queued(tmp_path):
    sink = SlowSink()
    fc = _fleet(tmp_path, sink, flush_budget_s=BUDGET)
    sent, took = _timed(fc.flush)
    assert BUDGET <= took <= BUDGET + DELAY + SLACK, took
    assert 0 < sent < N and fc.pending() == N - sent and fc.parked() == 0
    order = [json.loads(body)["events"][0]["i"] for body in sink.got]
    assert order == list(range(sent))
    assert fc.last_flush_budget_spent is True
    fc.flush(budget_s=60)
    assert fc.pending() == 0


def test_fleet_flush_honours_a_stop_request(tmp_path):
    sink = SlowSink()
    fc = _fleet(tmp_path, sink, flush_budget_s=60)
    fc.flush(should_stop=lambda: len(sink.got) >= 2)
    assert len(sink.got) == 2 and fc.pending() == N - 2


@pytest.mark.parametrize("bad", [0, -1, True, "15", math.nan, math.inf, None])
def test_fleet_flush_budget_is_validated(tmp_path, bad):
    with pytest.raises(ValueError, match="flush_budget_s"):
        FleetClient("http://central", outbox_path=tmp_path / "f", flush_budget_s=bad)


# --- config -----------------------------------------------------------------------


def _doc(**sections):
    d = {
        "node": {"id": "n"},
        "segments": [{"name": "a", "start_hz": 300e6, "stop_hz": 316e6}],
    }
    for k, v in sections.items():
        d.setdefault(k, {}).update(v)
    return d


def test_config_defaults():
    cfg = NodeConfig.from_dict(_doc())
    assert cfg.alert_flush_budget_s == 15.0
    assert cfg.shutdown_flush_s == 30.0
    fc = NodeConfig.from_dict(
        _doc(fleet={"base_url": "http://c", "auth": "legacy-token"})
    ).fleet
    assert fc is not None and fc.flush_budget_s == 15.0


def test_config_values_are_read():
    cfg = NodeConfig.from_dict(
        _doc(
            node={"shutdown_flush_s": 20},
            alerts={"flush_budget_s": 5},
            fleet={"base_url": "http://c", "auth": "legacy-token", "flush_budget_s": 7.5},
        )
    )
    assert cfg.shutdown_flush_s == 20 and cfg.alert_flush_budget_s == 5
    assert cfg.fleet is not None and cfg.fleet.flush_budget_s == 7.5


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("alerts", "flush_budget_s", 0),
        ("alerts", "flush_budget_s", -1),
        ("alerts", "flush_budget_s", 31),
        ("alerts", "flush_budget_s", "15"),
        ("node", "shutdown_flush_s", 0),
        ("node", "shutdown_flush_s", 46),
        ("fleet", "flush_budget_s", 0),
        ("fleet", "flush_budget_s", 100),
    ],
)
def test_config_rejects_out_of_range_budgets(section, key, value):
    extra = {"base_url": "http://c", "auth": "legacy-token"} if section == "fleet" else {}
    with pytest.raises(ConfigError) as e:
        NodeConfig.from_dict(_doc(**{section: {**extra, key: value}}))
    assert f"{section}.{key}" in str(e.value)


def test_a_slow_email_timeout_warns_that_the_stop_may_be_killed():
    cfg = NodeConfig.from_dict(
        _doc(
            node={"shutdown_flush_s": 45},
            alerts={"email": {"host": "smtp.example", "from": "a@example.com",
                              "to": ["b@example.com"], "timeout_s": 30}},
        )
    )
    ws = config_warnings(cfg)
    assert any("TimeoutStopSec" in w for w in ws), ws


def test_the_worst_case_tick_warns_when_it_nears_the_watchdog():
    cfg = NodeConfig.from_dict(
        _doc(
            alerts={"flush_budget_s": 30, "webhook_url": "https://h.example/x",
                    "email": {"host": "smtp.example", "from": "a@example.com",
                              "to": ["b@example.com"], "timeout_s": 20}},
            fleet={"base_url": "http://c", "auth": "legacy-token", "flush_budget_s": 30},
        )
    )
    ws = config_warnings(cfg)
    assert any("WatchdogSec" in w for w in ws), ws
    assert not any("WatchdogSec" in w for w in config_warnings(NodeConfig.from_dict(_doc())))


# --- the service ------------------------------------------------------------------


def _service(tmp_path, *, shutdown_flush_s, n_channels=2, fleet=True):
    cfg = NodeConfig.from_dict(
        _doc(node={"poll_s": 0.1, "baseline_cycles": 1, "shutdown_flush_s": shutdown_flush_s})
    )
    chans = []
    for c in range(n_channels):
        ob = Outbox(tmp_path / "alerts", channel=f"ch{c}", flush_budget_s=60)
        for i in range(N):
            ob.add({"i": i})
        chans.append(QueuedChannel(f"ch{c}", SlowSink(), ob))
    fc = _fleet(tmp_path, SlowSink(), flush_budget_s=60) if fleet else None
    return MonitorService(cfg, IdleDevice(), notifiers=chans, fleet=fc), chans, fc


class IdleDevice:
    def close(self):
        pass


def test_shutdown_flush_shares_one_budget_and_stays_under_it(tmp_path):
    svc, chans, fc = _service(tmp_path, shutdown_flush_s=0.4)
    _, took = _timed(lambda: svc.shutdown("SIGTERM"))
    # one deadline for all three sinks (not 3 x 0.4), plus one send in flight
    assert took <= 0.4 + DELAY + SLACK, took
    reopened = [Outbox(tmp_path / "alerts", channel=f"ch{c}") for c in range(2)]
    left = sum(o.pending() for o in reopened)
    left += FleetClient("http://c", outbox_path=tmp_path / "fleet").pending()
    assert left > 0  # the rest stays queued on disk for the next start


def test_a_stop_during_the_tick_flush_cuts_it_short(tmp_path):
    from quietroom.node.stopping import Stopper

    svc, chans, fc = _service(tmp_path, shutdown_flush_s=30, n_channels=1, fleet=False)
    stopper = Stopper()
    svc.stop_event = stopper
    sink = chans[0].send
    orig = sink.__call__

    def stop_after_two(p):
        orig(p)
        if len(sink.got) == 2:
            stopper.set("SIGTERM")

    chans[0].send = stop_after_two
    svc.flush_alerts()
    assert len(sink.got) == 2  # shutdown does the final flush under its own budget


def test_the_unit_gives_the_shutdown_budget_room():
    unit = Path(__file__).parents[1] / "packaging/systemd/quietroom-node.service"
    text = unit.read_text()
    assert "TimeoutStopSec=60" in text
    assert SHUTDOWN_FLUSH_BUDGET_S + 10 < 60


def test_every_production_sink_takes_the_flush_keywords():
    """The service passes budget_s/should_stop; a sink without them would fail
    every flush (recorded as a delivery error) and never deliver."""
    import inspect

    for flush in (QueuedChannel.flush, Outbox.flush, FleetClient.flush):
        params = inspect.signature(flush).parameters
        assert "budget_s" in params and "should_stop" in params, flush
