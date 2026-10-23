"""Anchor baseline, operator control requests (rebaseline / ack / promote-anchor).

The threat these pin down: a re-baselining node can be walked. Raise a planted
emitter a few dB per refresh period and every live sweep sees only a small step
over the *current* baseline, every refresh learns the step, and the bug is
never reported. The anchor is a long-lived reference baseline that refreshes
never move, so the cumulative change is visible and stops learning.
"""

import numpy as np
import pytest

from quietroom.alerts import AlertEngine, AlertRule
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.node.rebaseline import IncidentGate, RecaptureReason
from quietroom.node.service import MonitorService
from quietroom.radio.device import Device, DeviceInfo
from quietroom.store import Store
from test_rebaseline import BUG_BIN, Clock, cfg

OTHER_BIN = 30
BIN_HZ = 25000
SEG_START = 300_000_000
BUG_HZ = SEG_START + BUG_BIN * BIN_HZ
OTHER_HZ = SEG_START + OTHER_BIN * BIN_HZ


class CreepRoom(Device):
    """Fake radio: per-bin excess power (dB over the floor) on *all* sweeps."""

    def __init__(self, lna_gain=16):
        self.floor = -95.0
        self.excess: dict[int, float] = {}
        self.lna_gain = lna_gain

    def probe(self):
        return DeviceInfo("s", "Room", "1")

    def frontend_settings(self):
        return {"lna_gain": self.lna_gain}

    def sweep(self, plan, cycles=1):
        f = np.arange(plan.start_hz, plan.stop_hz, plan.bin_hz, dtype=float)
        rng = np.random.default_rng(0)
        for c in range(cycles):
            p = self.floor + rng.normal(0, 0.5, len(f))
            for b, db in self.excess.items():
                p[b] = self.floor + db
            yield PowerSpectrum(f, p, float(c))

    def capture_iq(self, c, s, n):
        return np.zeros(n, dtype=np.complex64)


def svc_for(store, dev, clock, events=None, **baseline):
    baseline.setdefault("freeze_score", 50)
    return MonitorService(
        cfg(**baseline), dev, store=store, clock=clock,
        sink=(events.append if events is not None else None),
    )


def versions(store):
    return store.baseline_versions("n1", "uhf")


def mean_at(svc, b=BUG_BIN):
    return float(svc.baselines["uhf"].mean_dbm[b])


# --- IncidentGate: per-emitter holds ---------------------------------------


def test_gate_releases_only_the_acknowledged_emitter():
    g = IncidentGate(hold_s=300)
    g.note_hit("uhf", 100.0, BUG_HZ)
    g.note_hit("uhf", 110.0, OTHER_HZ)
    assert g.is_frozen("uhf", 120.0)
    assert g.release("uhf", 115.0, freq_hz=BUG_HZ + 10_000) == [BUG_HZ]
    assert g.is_frozen("uhf", 120.0)  # the other emitter still holds the segment
    assert g.frozen_until("uhf") == 410.0
    g.release("uhf", 115.0)  # segment-wide
    assert not g.is_frozen("uhf", 120.0)


def test_gate_release_never_covers_hits_after_the_ack():
    g = IncidentGate(hold_s=300)
    g.note_hit("uhf", 100.0, BUG_HZ)
    g.note_hit("uhf", 200.0, BUG_HZ)  # still transmitting after the operator looked
    assert g.release("uhf", 150.0, freq_hz=BUG_HZ) == []
    assert g.frozen_until("uhf") == 500.0


# --- anchor ------------------------------------------------------------------


def test_first_baseline_becomes_the_anchor(tmp_path):
    store = Store(tmp_path / "q.db")
    svc = svc_for(store, CreepRoom(), Clock())
    svc.start()
    a = store.anchor("n1", "uhf", svc.fingerprints["uhf"])
    assert a["version"] == 1 and a["reason"] == "initial"
    h = svc.health()["anchors"]["uhf"]
    assert h["version"] == 1 and h["divergence"] == []


def creep(store, clock, room, svc, steps, step_db=3.0, period=110.0):
    """Raise the planted emitter step_db per max-age period; return live findings."""
    live = []
    for i in range(1, steps + 1):
        room.excess[BUG_BIN] = step_db * i
        clock.t += period
        r = svc.tick(clock.t)
        live.append(r.findings)
    return live


def test_slow_creep_is_caught_by_the_anchor_and_never_learned(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock, events = CreepRoom(), Clock(1000.0), []
    svc = svc_for(store, room, clock, events, max_age_s=100, incident_hold_s=600)
    svc.start()
    live = creep(store, clock, room, svc, steps=15)
    # The attack works against the live detector: no step is ever reported live...
    assert sum(live[:3]) == 0
    div = [e for e in events if e["type"] == "anchor_divergence"]
    # ...but the anchor sees the cumulative change and refuses to learn past it.
    assert div, "creep was absorbed without an anchor_divergence event"
    assert all(abs(f["freq_hz"] - BUG_HZ) < 50e3 for e in div for f in e["findings"])
    assert div[0]["anchor_version"] == 1
    held = svc.health()["anchors"]["uhf"]
    assert held["version"] == 1
    # learning stopped: the stored baseline sits far below the emitter's level now
    assert mean_at(svc) < room.floor + 20
    # held candidates are reported; once learning stops, the (now un-absorbed)
    # emitter also crosses the live threshold and the ordinary incident hold takes over
    assert any(abs(f["freq_hz"] - BUG_HZ) < 50e3 for f in held["held_candidate"])
    assert sum(live) > 0


def test_small_cumulative_change_keeps_refreshing_normally(tmp_path):
    """No false hold: change well inside the noise of the anchor is learned."""
    store = Store(tmp_path / "q.db")
    room, clock, events = CreepRoom(), Clock(1000.0), []
    svc = svc_for(store, room, clock, events, max_age_s=100, incident_hold_s=600)
    svc.start()
    live = creep(store, clock, room, svc, steps=2, step_db=2.0)
    assert live == [0, 0] and versions(store) == [1, 2, 3]
    assert not [e for e in events if e["type"] == "anchor_divergence"]


def test_anchor_divergence_goes_through_the_alert_engine(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock, sent = CreepRoom(), Clock(1000.0), []
    svc = MonitorService(
        cfg(max_age_s=100, freeze_score=50, incident_hold_s=600), room, store=store,
        clock=clock, alert_engine=AlertEngine([AlertRule(min_score=50)]),
        notifiers=[sent.append],
    )
    svc.start()
    creep(store, clock, room, svc, steps=15)
    kinds = {a.get("event_type") for a in sent}
    assert "anchor_divergence" in kinds


def test_anchor_survives_restart_and_is_kept_per_front_end(tmp_path):
    store = Store(tmp_path / "q.db")
    clock = Clock(1000.0)
    svc = svc_for(store, CreepRoom(16), clock, max_age_s=100)
    svc.start()
    clock.t = 1200.0
    svc.tick(clock.t)
    assert versions(store) == [1, 2]
    svc2 = svc_for(store, CreepRoom(16), clock, max_age_s=100)
    svc2.start()
    assert svc2.health()["anchors"]["uhf"]["version"] == 1
    svc3 = svc_for(store, CreepRoom(24), clock, max_age_s=100)  # new front end
    svc3.start()
    assert svc3.health()["anchors"]["uhf"]["version"] == 3  # its own reference
    svc4 = svc_for(store, CreepRoom(16), clock, max_age_s=100)  # swapped back
    svc4.start()
    assert svc4.health()["anchors"]["uhf"]["version"] == 1


def test_upgrade_adopts_earliest_same_front_end_baseline_as_anchor(tmp_path):
    store = Store(tmp_path / "q.db")
    clock = Clock(1000.0)
    svc = svc_for(store, CreepRoom(), clock, max_age_s=100)
    svc.start()
    clock.t = 1200.0
    svc.tick(clock.t)
    store.db.execute("DELETE FROM baseline_anchors")  # as written by an older release
    store.db.commit()
    svc2 = svc_for(store, CreepRoom(), clock, max_age_s=1000)
    svc2.start()
    a = svc2.health()["anchors"]["uhf"]
    assert a["version"] == 1 and "earliest" in a["reason"]


# --- operator control requests ---------------------------------------------


def test_manual_rebaseline_request_is_applied_on_the_next_tick(tmp_path):
    store = Store(tmp_path / "q.db")
    clock, events = Clock(1000.0), []
    svc = svc_for(store, CreepRoom(), clock, events)
    svc.start()
    rid = store.request_control("n1", "uhf", "rebaseline", {"note": "moved desk"}, now=1005.0)
    svc.tick(1010.0)
    req = store.control_request(rid)
    assert req["status"] == "done" and req["result"]["version"] == 2
    meta = store.baseline_meta("n1", "uhf")
    assert meta.reason == RecaptureReason.MANUAL == "manual"
    assert meta.detail["request_id"] == rid and meta.detail["note"] == "moved desk"
    assert meta.detail["forced"] is False
    ctl = [e for e in events if e["type"] == "control"]
    assert ctl and ctl[-1]["status"] == "done" and ctl[-1]["request_id"] == rid


def bug_live(svc, room, clock, t):
    room.excess[BUG_BIN] = 65.0
    clock.t = t
    assert svc.tick(t).findings >= 1


def test_manual_rebaseline_is_refused_during_an_incident(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, incident_hold_s=300)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    rid = store.request_control("n1", "uhf", "rebaseline", {}, now=1015.0)
    svc.tick(1020.0)
    req = store.control_request(rid)
    assert req["status"] == "refused"
    why = req["result"]["why"]
    assert "frozen until 1970-01-01T00:21:50Z (290 s)" in why and "--force" in why
    assert versions(store) == [1]
    room.excess.clear()
    svc.tick(2000.0)  # hold long over: a refused request is never replayed later
    assert versions(store) == [1]


def test_forced_rebaseline_learns_during_an_incident_but_anchor_still_flags(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock, events = CreepRoom(), Clock(1000.0), []
    svc = svc_for(store, room, clock, events, max_age_s=100, incident_hold_s=300)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    rid = store.request_control(
        "n1", "uhf", "rebaseline", {"force": True, "note": "new AP in ceiling"}, now=1015.0
    )
    svc.tick(1020.0)
    req = store.control_request(rid)
    assert req["status"] == "done" and req["result"]["forced"] is True
    meta = store.baseline_meta("n1", "uhf")
    assert meta.detail["forced"] is True and meta.detail["frozen_until"] == 1310.0
    assert any(abs(m * 1e6 - BUG_HZ) < 50e3 for m in meta.detail["alert_grade_at_capture_mhz"])
    assert mean_at(svc) > -40  # the operator asked for it: the emitter is learned...
    anchor = svc.health()["anchors"]["uhf"]
    assert anchor["version"] == 1
    assert any(abs(f["freq_hz"] - BUG_HZ) < 50e3 for f in anchor["divergence"])  # ...not hidden
    assert any(e["type"] == "anchor_divergence" for e in events)
    # and automatic refreshes that still carry it stay held until it is promoted
    for t in (1500.0, 1700.0, 1900.0):
        svc.tick(t)
    assert versions(store) == [1, 2]


def test_promote_anchor_accepts_the_current_baseline_as_the_reference(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=300)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    store.request_control("n1", "uhf", "rebaseline", {"force": True}, now=1015.0)
    svc.tick(1020.0)
    rid = store.request_control("n1", "uhf", "promote-anchor", {"note": "AP approved"}, now=1025.0)
    svc.tick(1030.0)
    assert store.control_request(rid)["status"] == "done"
    a = svc.health()["anchors"]["uhf"]
    assert a["version"] == 2 and a["divergence"] == [] and a["reason"] == "promoted"
    assert store.control_request(rid)["result"]["accepted_mhz"] == [300.2]
    # Accepting the emitter also ends the incident hold it caused (live, capture and
    # anchor hits up to the request); otherwise refreshes would stay frozen an hour.
    assert "uhf" not in svc.health()["incident_hold"]
    svc.tick(1130.0)
    assert versions(store) == [1, 2, 3]
    assert svc.health()["anchors"]["uhf"]["divergence"] == []
    svc2 = svc_for(store, room, clock, max_age_s=1000, incident_hold_s=300)
    svc2.start()
    assert svc2.health()["anchors"]["uhf"]["version"] == 2
    assert "uhf" not in svc2.health()["incident_hold"]  # the release survives a restart


def test_ack_releases_the_hold_early_once_the_emitter_is_gone(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    room.excess.clear()  # sweep team removed the device
    svc.tick(1200.0)
    assert versions(store) == [1] and "uhf" in svc.health()["rebaseline_deferred"]
    assert "uhf" in svc.health()["incident_hold"]
    rid = store.request_control("n1", "uhf", "ack", {"freq_hz": BUG_HZ}, now=1210.0)
    svc.tick(1220.0)  # an hour early
    req = store.control_request(rid)
    assert req["status"] == "done" and req["result"]["released_mhz"]
    assert versions(store) == [1, 2]
    assert mean_at(svc) < -80  # clean room learned, not the bug
    assert "uhf" not in svc.health()["incident_hold"]


def test_ack_does_not_release_a_different_active_emitter(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc.start()
    room.excess.update({BUG_BIN: 65.0, OTHER_BIN: 65.0})
    svc.tick(1010.0)
    room.excess.clear()
    store.request_control("n1", "uhf", "ack", {"freq_hz": BUG_HZ}, now=1100.0)
    svc.tick(1200.0)
    assert versions(store) == [1]
    hold = svc.health()["incident_hold"]["uhf"]
    assert [round(e["freq_hz"]) for e in hold["emitters"]] == [OTHER_HZ]


def test_ack_while_still_transmitting_refreezes_on_the_next_hit(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    store.request_control("n1", "uhf", "ack", {}, now=1100.0)
    bug_live(svc, room, clock, 1200.0)  # still on air
    assert versions(store) == [1]
    assert svc.baselines["uhf"].mean_dbm[BUG_BIN] < -80


def test_ack_is_honoured_after_a_restart(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    room.excess.clear()
    store.request_control("n1", "uhf", "ack", {"freq_hz": BUG_HZ}, now=1050.0)
    svc.tick(1060.0)
    clock.t = 1200.0
    svc2 = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc2.start()  # findings replayed from the store, minus the acknowledged emitter
    assert versions(store) == [1, 2] and "uhf" not in svc2.health()["incident_hold"]


def test_incident_hold_still_restored_after_restart_without_ack(tmp_path):
    store = Store(tmp_path / "q.db")
    room, clock = CreepRoom(), Clock(1000.0)
    svc = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc.start()
    bug_live(svc, room, clock, 1010.0)
    clock.t = 1200.0
    svc2 = svc_for(store, room, clock, max_age_s=100, incident_hold_s=3600)
    svc2.start()
    assert versions(store) == [1]
    hold = svc2.health()["incident_hold"]["uhf"]
    assert hold["frozen_until"] == 4610.0


def test_requests_for_unknown_segments_or_commands_fail_cleanly(tmp_path):
    store = Store(tmp_path / "q.db")
    svc = svc_for(store, CreepRoom(), Clock(1000.0))
    svc.start()
    a = store.request_control("n1", "nope", "rebaseline", {}, now=1001.0)
    with pytest.raises(ValueError):
        store.request_control("n1", "uhf", "explode", {}, now=1001.0)
    # a row written by some newer tool: the node must fail it, not crash
    b = store.db.execute(
        "INSERT INTO control_requests(node_id,segment,command,requested_at)"
        " VALUES('n1','uhf','explode',1001.0)"
    ).lastrowid
    store.db.commit()
    c = store.request_control("other-node", "uhf", "rebaseline", {}, now=1001.0)
    svc.tick(1010.0)
    assert store.control_request(a)["status"] == "failed"
    assert store.control_request(b)["status"] == "failed"
    assert store.control_request(c)["status"] == "pending"  # not ours
    assert svc.health()["last_error"] is None
    assert versions(store) == [1]


@pytest.mark.parametrize("cmd", ["rebaseline", "ack", "promote-anchor"])
def test_store_round_trips_control_requests(tmp_path, cmd):
    store = Store(tmp_path / "q.db")
    rid = store.request_control("n1", "uhf", cmd, {"note": "x"}, now=5.0)
    assert [r["id"] for r in store.pending_controls("n1")] == [rid]
    store.finish_control(rid, "done", {"ok": 1}, now=6.0)
    r = store.control_request(rid)
    got = (r["status"], r["result"], r["done_at"], r["args"])
    assert got == ("done", {"ok": 1}, 6.0, {"note": "x"})
    assert store.pending_controls("n1") == []


def test_heartbeat_carries_anchor_and_hold_state_to_central():
    from quietroom.fleet import NodeIdentity, heartbeat_payload

    health = {
        "anchors": {"uhf": {"version": 1, "divergence": [{"freq_hz": 3e8}]}},
        "incident_hold": {"uhf": {"frozen_until": 9.0, "emitters": []}},
        "rebaseline_deferred": {"uhf": {"reason": "max_age"}},
    }
    hb = heartbeat_payload(NodeIdentity("n1", "hq", "3", "lab"), health, 1.0)
    assert hb["anchors"] == health["anchors"]
    assert hb["incident_hold"] == health["incident_hold"]
    assert hb["rebaseline_deferred"] == health["rebaseline_deferred"]
    assert heartbeat_payload(NodeIdentity("n1", "hq", "3", "lab"), {}, 1.0)["anchors"] == {}
