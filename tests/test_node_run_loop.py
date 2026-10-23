"""The daemon loop must sleep poll_s between ticks (it busy-looped since #38)."""

from __future__ import annotations

import pytest

from quietroom.config import default_config
from quietroom.node import service as service_mod
from quietroom.node.service import MonitorService


class _Stop(Exception):
    pass


class _Fleet:
    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        return 0

    def pending(self):
        return 1  # a backlog the node can't deliver (central down, or this node revoked)

    def send_events(self, *a):
        return False

    def send_heartbeat(self, *a):
        return False


def test_the_daemon_loop_sleeps_poll_s_between_ticks(monkeypatch):
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise _Stop

    monkeypatch.setattr(service_mod.time, "sleep", fake_sleep)
    fleet = _Fleet()
    svc = MonitorService(default_config(), None, fleet=fleet)
    monkeypatch.setattr(svc, "start", lambda: None)
    svc.scheduler.due = lambda now, last: []  # nothing due: the case that spun
    with pytest.raises(_Stop):
        svc.run()  # how quietroom-node calls it: no stop_event, no max_ticks
    assert sleeps == [svc.config.poll_s] * 3
    # one fleet flush (one network attempt) per poll interval, not thousands per second
    assert fleet.flushes == 3
