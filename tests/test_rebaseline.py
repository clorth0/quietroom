"""Re-baseline policy: max age, drift, fingerprint invalidation, incident freeze."""

import json
import sqlite3

import numpy as np
import pytest

from quietroom.config import NodeConfig
from quietroom.node.rebaseline import (
    RebaselinePolicy,
    RecaptureReason,
    frontend_fingerprint,
)
from quietroom.node.service import MonitorService
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.radio.device import Device, DeviceInfo
from quietroom.store import Store

BUG_BIN = 8


class Room(Device):
    """Fake radio whose RF environment the test controls."""

    def __init__(self, lna_gain=16):
        self.floor = -95.0
        self.bug = False  # planted emitter in live (cycles == 1) sweeps
        self.bug_in_capture = False  # planted emitter during baseline capture
        self.lna_gain = lna_gain
        self.captures = 0

    def probe(self):
        return DeviceInfo("s", "Room", "1")

    def frontend_settings(self):
        return {"lna_gain": self.lna_gain}

    def sweep(self, plan, cycles=1):
        if cycles > 1:
            self.captures += 1
        f = np.arange(plan.start_hz, plan.stop_hz, plan.bin_hz, dtype=float)
        rng = np.random.default_rng(0)
        for c in range(cycles):
            p = self.floor + rng.normal(0, 0.5, len(f))
            if (cycles == 1 and self.bug) or (cycles > 1 and self.bug_in_capture):
                p[BUG_BIN] = -30.0
            yield PowerSpectrum(f, p, float(c))

    def capture_iq(self, c, s, n):
        return np.zeros(n, dtype=np.complex64)


def cfg(antenna="ANT500", **baseline):
    return NodeConfig.from_dict(
        {
            "node": {"id": "n1", "antenna": antenna, "baseline_cycles": 3},
            "baseline": baseline,
            "segments": [
                {
                    "name": "uhf",
                    "start_hz": 300_000_000,
                    "stop_hz": 301_000_000,
                    "bin_hz": 25000,
                    "interval_s": 10,
                }
            ],
        }
    )


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def svc_for(store, dev, clock, **baseline):
    baseline.setdefault("freeze_score", 50)
    return MonitorService(cfg(**baseline), dev, store=store, clock=clock)


def versions(store):
    return store.baseline_versions("n1", "uhf")


# --- policy / config -------------------------------------------------------


def test_policy_defaults_preserve_reuse_and_validate():
    p = RebaselinePolicy()
    assert p.max_age_s is None and p.drift_window == 0
    with pytest.raises(ValueError):
        RebaselinePolicy(max_age_s=0)
    with pytest.raises(ValueError):
        RebaselinePolicy(drift_window=1)  # one sweep is not a baseline
    with pytest.raises(ValueError):
        RebaselinePolicy(incident_hold_s=-1)


def test_config_baseline_section_parses():
    c = cfg(max_age_s=86400, drift_window=5, drift_mean_db=2.5, incident_hold_s=600)
    p = c.rebaseline
    assert (p.max_age_s, p.drift_window, p.drift_mean_db, p.incident_hold_s) == (
        86400,
        5,
        2.5,
        600,
    )
    assert c.antenna == "ANT500"
    # freeze_score defaults to the alert threshold so "alerting" means one thing
    c2 = NodeConfig.from_dict(
        {
            "node": {"id": "n"},
            "alerts": {"min_score": 70},
            "segments": [{"name": "s", "start_hz": 300_000_000, "stop_hz": 301_000_000}],
        }
    )
    assert c2.rebaseline.freeze_score == 70


def test_fingerprint_covers_plan_frontend_and_antenna():
    c = cfg()
    plan = c.segments[0].plan
    base = frontend_fingerprint(plan, Room(16), DeviceInfo("s", "Room", "1"), "ANT500")
    assert base == frontend_fingerprint(plan, Room(16), DeviceInfo("s", "Room", "1"), "ANT500")
    assert base != frontend_fingerprint(plan, Room(24), DeviceInfo("s", "Room", "1"), "ANT500")
    assert base != frontend_fingerprint(plan, Room(16), DeviceInfo("s", "Room", "1"), "whip")
    assert base != frontend_fingerprint(plan, Room(16), DeviceInfo("s2", "Room", "1"), "ANT500")


# --- triggers --------------------------------------------------------------


def test_initial_capture_is_versioned_with_reason(tmp_path):
    store = Store(tmp_path / "q.db")
    svc_for(store, Room(), Clock()).start()
    meta = store.baseline_meta("n1", "uhf")
    assert meta.version == 1 and meta.reason == RecaptureReason.INITIAL
    assert meta.captured_at == 1000.0 and meta.fingerprint


def test_max_age_recaptures_on_start_and_during_ticks(tmp_path):
    store = Store(tmp_path / "q.db")
    clock = Clock(1000.0)
    svc_for(store, Room(), clock, max_age_s=100).start()
    clock.t = 1050.0
    svc_for(store, Room(), clock, max_age_s=100).start()
    assert versions(store) == [1]  # young enough: reused
    clock.t = 1200.0
    svc = svc_for(store, Room(), clock, max_age_s=100)
    svc.start()
    assert versions(store) == [1, 2]
    meta = store.baseline_meta("n1", "uhf")
    assert meta.reason == RecaptureReason.MAX_AGE
    assert meta.detail["previous_version"] == 1 and meta.detail["age_s"] == 200.0
    clock.t = 1350.0
    svc.tick(clock.t)
    assert versions(store) == [1, 2, 3]


def test_frontend_change_invalidates_stored_baseline(tmp_path):
    store = Store(tmp_path / "q.db")
    svc_for(store, Room(lna_gain=16), Clock()).start()
    svc_for(store, Room(lna_gain=24), Clock()).start()
    meta = store.baseline_meta("n1", "uhf")
    assert versions(store) == [1, 2] and meta.reason == RecaptureReason.FINGERPRINT
    assert meta.detail["previous_fingerprint"] != meta.fingerprint
    # antenna swap recorded in config is an invalidation too
    MonitorService(cfg(antenna="whip", freeze_score=50), Room(24), store=store,
                   clock=Clock()).start()
    assert versions(store) == [1, 2, 3]


def test_legacy_row_without_fingerprint_is_recaptured_once(tmp_path):
    store = Store(tmp_path / "q.db")
    svc_for(store, Room(), Clock()).start()
    store.db.execute("UPDATE baselines SET fingerprint=''")
    store.db.commit()
    svc_for(store, Room(), Clock()).start()
    svc_for(store, Room(), Clock()).start()
    assert versions(store) == [1, 2]
    assert store.baseline_meta("n1", "uhf").detail["previous_fingerprint"] == ""


def test_drift_triggers_recapture_with_report(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room()
    clock = Clock()
    svc = svc_for(store, room, clock, drift_window=3, drift_mean_db=3.0)
    svc.start()
    room.floor = -90.0  # +5 dB everywhere: below the z-threshold, but the room moved
    for _ in range(3):
        clock.t += 10
        r = svc.tick(clock.t)
        assert r.findings == 0
    meta = store.baseline_meta("n1", "uhf")
    assert versions(store) == [1, 2] and meta.reason == RecaptureReason.DRIFT
    assert meta.detail["drift"]["mean_delta_db"] >= 3.0
    np.testing.assert_allclose(svc.baselines["uhf"].mean_dbm, -90.0, atol=1.0)


def test_small_drift_does_not_recapture(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room()
    svc = svc_for(store, room, Clock(), drift_window=3, drift_mean_db=3.0)
    svc.start()
    room.floor = -94.0
    for i in range(6):
        svc.tick(2000.0 + 10 * i)
    assert versions(store) == [1]


# --- incident freeze -------------------------------------------------------


def test_active_incident_freezes_learning_until_hold_expires(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room()
    clock = Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=300)
    svc.start()
    room.bug = True
    clock.t = 1010.0
    assert svc.tick(clock.t).findings >= 1  # incident begins
    clock.t = 1200.0  # baseline is now over-age, but the bug is still on air
    svc.tick(clock.t)
    assert versions(store) == [1]
    deferred = svc.health()["rebaseline_deferred"]["uhf"]
    assert deferred["reason"] == "max_age" and "incident" in deferred["why"]
    room.bug = False
    clock.t = 1400.0  # quiet but still inside the hold window from the last hit
    svc.tick(clock.t)
    assert versions(store) == [1]
    clock.t = 1600.0
    svc.tick(clock.t)
    assert versions(store) == [1, 2]
    assert svc.baselines["uhf"].mean_dbm[BUG_BIN] < -80.0  # bug never learned
    assert "uhf" not in svc.health()["rebaseline_deferred"]


def test_drift_window_ignores_sweeps_during_incident(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room()
    svc = svc_for(store, room, Clock(), drift_window=3, drift_mean_db=0.1, incident_hold_s=1e9)
    svc.start()
    room.bug = True
    for i in range(6):
        svc.tick(2000.0 + 10 * i)
    assert versions(store) == [1]


def test_emitter_appearing_during_recapture_aborts_it(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room()
    clock = Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=300)
    svc.start()
    room.bug_in_capture = True  # live tick is clean, the capture sweeps are not
    clock.t = 1200.0
    svc.tick(clock.t)
    assert versions(store) == [1]
    assert "during capture" in svc.health()["rebaseline_deferred"]["uhf"]["why"]
    assert svc.baselines["uhf"].mean_dbm[BUG_BIN] < -80.0


def test_incident_freeze_survives_restart_via_store(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room()
    clock = Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=300)
    svc.start()
    room.bug = True
    clock.t = 1010.0
    svc.tick(clock.t)
    # restart: stored baseline is over-age, last alert-grade finding 140 s ago
    clock.t = 1150.0
    svc2 = svc_for(store, room, clock, max_age_s=100, incident_hold_s=300)
    svc2.start()
    assert versions(store) == [1]
    assert "uhf" in svc2.baselines  # keeps monitoring with the old baseline


def test_fingerprint_change_during_incident_drops_invalid_baseline(tmp_path):
    store = Store(tmp_path / "q.db")
    room = Room(16)
    clock = Clock(1000.0)
    svc = svc_for(store, room, clock, incident_hold_s=300)
    svc.start()
    room.bug = True
    clock.t = 1010.0
    svc.tick(clock.t)
    clock.t = 1020.0
    svc2 = svc_for(store, Room(24), clock, incident_hold_s=300)
    svc2.start()
    assert versions(store) == [1]
    assert "uhf" not in svc2.baselines  # never score against a mismatched front end
    assert svc2.health()["rebaseline_deferred"]["uhf"]["reason"] == "fingerprint_changed"


def test_no_baseline_saved_while_incident_active(tmp_path):
    """Invariant check over a long scripted run: every save happens outside a hold."""
    store = Store(tmp_path / "q.db")
    saves = []
    real = store.save_baseline

    def spy(*a, **kw):
        saves.append(clock.t)
        return real(*a, **kw)

    store.save_baseline = spy
    room = Room()
    clock = Clock(0.0)
    svc = svc_for(store, room, clock, max_age_s=50, incident_hold_s=120, drift_window=3)
    svc.start()
    hits = []
    for step in range(1, 120):
        clock.t = step * 10.0
        room.bug = 30 <= step < 45 or 80 <= step < 90
        r = svc.tick(clock.t)
        if r.findings:
            hits.append(clock.t)
    for t in saves[1:]:
        assert all(not (h <= t < h + 120) for h in hits), (t, hits)
    assert len(saves) > 3


def test_rebaseline_event_emitted(tmp_path):
    store = Store(tmp_path / "q.db")
    events = []
    clock = Clock(1000.0)
    svc = MonitorService(cfg(max_age_s=100, freeze_score=50), Room(), sink=events.append,
                         store=store, clock=clock)
    svc.start()
    clock.t = 1200.0
    svc.tick(clock.t)
    kinds = [e for e in events if e["type"] == "baseline"]
    assert [e["reason"] for e in kinds] == ["initial", "max_age"]
    assert kinds[-1]["version"] == 2


# --- store -----------------------------------------------------------------


def test_store_migrates_pre_policy_schema(tmp_path):
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.executescript(
        "CREATE TABLE baselines(id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT,"
        " version INTEGER, created_at REAL, sweep_count INTEGER, blob BLOB,"
        " UNIQUE(node_id,segment,version));"
    )
    db.commit()
    db.close()
    s = Store(p)
    cols = {r[1] for r in s.db.execute("PRAGMA table_info(baselines)")}
    assert {"reason", "fingerprint", "captured_at", "detail"} <= cols
    assert s.baseline_meta("n1", "uhf") is None


def test_store_last_incident_at(tmp_path):
    s = Store(tmp_path / "q.db")
    spec = PowerSpectrum(np.array([1.0, 2.0]), np.array([-90.0, -90.0]), 5.0)
    s.record_sweep("n1", "uhf", spec, [{"freq_hz": 1.0, "score": 40.0}])
    assert s.last_incident_at("n1", "uhf", min_score=50) is None
    s.record_sweep("n1", "uhf", spec, [{"freq_hz": 1.0, "score": 90.0}],
                   timestamp=123.0)
    assert s.last_incident_at("n1", "uhf", min_score=50) == 123.0


def test_demo_node_once_reports_the_planted_bug(capsys):
    from quietroom.node.cli import main

    assert main(["--demo", "--once"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    sweeps = [e for e in lines if e.get("type") == "sweep" and e["segment"] == "covert-uhf"]
    assert sweeps and any(abs(f["freq_hz"] - 308e6) < 50e3 for f in sweeps[0]["findings"])
    health = lines[-1]["health"]
    assert health["last_error"] is None


def test_failed_initial_capture_is_retried_on_a_later_tick(tmp_path):
    store = Store(tmp_path / "q.db")

    class Flaky(Room):
        def sweep(self, plan, cycles=1):
            if cycles > 1 and self.captures == 0:
                self.captures += 1
                raise RuntimeError("usb hiccup")
            yield from super().sweep(plan, cycles)

    svc = svc_for(store, Flaky(), Clock())
    svc.start()
    assert versions(store) == [] and "usb hiccup" in svc.health()["last_error"]
    svc.tick(1010.0)
    assert versions(store) == [1]
    assert store.baseline_meta("n1", "uhf").reason == RecaptureReason.INITIAL
