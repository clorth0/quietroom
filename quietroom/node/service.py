"""Headless monitoring service: baselines per segment, scheduled sweeps, events.

Events are plain dicts delivered to a sink callable (and to the Store, alert
engine and fleet client when configured). Baselines are versioned in the Store
and refreshed under quietroom.node.rebaseline's policy; learning is frozen
while a segment has an active incident.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any
from quietroom.bands import bands_for
from quietroom.config import SegmentConfig
from quietroom.engine.baseline import build_baseline, drift_report
from quietroom.engine.persistence import PersistenceTracker
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum
from quietroom.node.rebaseline import (
    IncidentGate,
    RecaptureReason,
    drift_exceeded,
    frontend_fingerprint,
    stored_trigger,
)
from quietroom.node.scheduler import BandScheduler
from quietroom.radio.device import DeviceError
from quietroom.store import BaselineMeta


@dataclass
class TickResult:
    segments_run: list = field(default_factory=list)
    findings: int = 0
    errors: list = field(default_factory=list)


class MonitorService:
    def __init__(
        self,
        config,
        device,
        sink=None,
        store=None,
        alert_engine=None,
        notifiers=(),
        fleet=None,
        fleet_interval_s=60.0,
        notifier=None,
        clock=None,
    ) -> None:
        self.config = config
        self.device = device
        self.sink = sink or (lambda e: None)
        self.store = store
        self.alert_engine = alert_engine
        self.notifiers = list(notifiers)
        self.fleet = fleet
        self.fleet_interval_s = fleet_interval_s
        self.notifier = notifier
        self._last_heartbeat = 0.0
        self.delivery_errors: dict[str, str] = {}
        self.scheduler = BandScheduler(config.segments)
        self.baselines: dict[str, Baseline] = {}
        self.trackers: dict[str, PersistenceTracker] = {}
        self.last_run: dict[str, float] = {}
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.clock = clock or time.time
        self.policy = config.rebaseline
        self.gate = IncidentGate(self.policy.incident_hold_s)
        self.fingerprints: dict[str, str] = {}
        self.baseline_meta: dict[str, BaselineMeta] = {}
        self.rebaseline_deferred: dict[str, dict[str, Any]] = {}
        self._drift_buf: dict[str, list[PowerSpectrum]] = {}

    def start(self):
        info = self.device.probe()
        if info is None:
            raise DeviceError("radio not detected")
        self.device_info = info
        self.started_at = self.clock()
        self.capture_baselines()
        return info

    def _emit(self, event: dict[str, Any]) -> None:
        self.sink(event)
        if self.fleet is not None:
            self.fleet.send_events(self.config.node_id, [event])

    def capture_baselines(self):
        now = self.clock()
        for seg in self.config.segments:
            if not seg.enabled:
                continue
            try:
                fp = frontend_fingerprint(
                    seg.plan, self.device, getattr(self, "device_info", None), self.config.antenna
                )
                self.fingerprints[seg.name] = fp
                meta = None
                if self.store is not None:
                    last = self.store.last_incident_at(
                        self.config.node_id, seg.name, self.policy.freeze_score
                    )
                    if last is not None:
                        self.gate.note_hit(seg.name, last)
                    meta = self.store.baseline_meta(self.config.node_id, seg.name)
                if meta is None:
                    self._recapture(seg, now, RecaptureReason.INITIAL, {})
                    continue
                self.baselines[seg.name] = self.store.load_baseline(self.config.node_id, seg.name)
                self.trackers[seg.name] = PersistenceTracker()
                self.baseline_meta[seg.name] = meta
                trigger = stored_trigger(self.policy, meta, now, fp)
                if trigger is not None:
                    self._recapture(seg, now, *trigger)
            except Exception as e:
                self.last_error = f"baseline {seg.name}: {e}"

    def _defer(
        self, seg: SegmentConfig, reason: RecaptureReason, detail: dict[str, Any], why: str
    ) -> None:
        self.rebaseline_deferred[seg.name] = {
            "reason": str(reason),
            "detail": detail,
            "why": why,
            "frozen_until": self.gate.frozen_until(seg.name),
        }
        self._drift_buf.pop(seg.name, None)

    def _alert_grade(self, findings: list[Finding]) -> list[Finding]:
        return [f for f in findings if f.score >= self.policy.freeze_score]

    def _recapture(
        self, seg: SegmentConfig, now: float, reason: RecaptureReason, detail: dict[str, Any]
    ) -> bool:
        """Capture and version a new baseline, unless learning is frozen.

        Returns True if a new baseline was adopted. The two guards are the
        module invariant: no capture while an incident holds the segment, and
        a capture whose own sweeps show an alert-grade emitter (scored against
        the baseline it would replace) is discarded rather than learned.
        """
        if self.gate.is_frozen(seg.name, now):
            if reason is RecaptureReason.FINGERPRINT:
                # A baseline from another front end is not evidence of anything;
                # stop scoring against it rather than raise false incidents.
                self.baselines.pop(seg.name, None)
                self.trackers.pop(seg.name, None)
            until = self.gate.frozen_until(seg.name)
            self._defer(seg, reason, detail, f"incident active; learning frozen until {until}")
            return False
        old = None if reason is RecaptureReason.FINGERPRINT else self.baselines.get(seg.name)
        spectra = list(self.device.sweep(seg.plan, cycles=self.config.baseline_cycles))
        if not spectra:
            raise DeviceError(f"no baseline sweeps for {seg.name}")
        if old is not None:
            for s in spectra:
                hits = self._alert_grade(
                    sweep_findings(
                        s, old, seg.plan, k=self.config.threshold_k,
                        bands=bands_for(self.config.region),
                    )
                )
                if hits:
                    self.gate.note_hit(seg.name, now)
                    mhz = hits[0].candidate.center_freq_hz / 1e6
                    self._defer(
                        seg, reason, detail,
                        f"incident during capture: alert-grade emitter at {mhz:.3f} MHz",
                    )
                    return False
        new = build_baseline(spectra)
        fp = self.fingerprints.get(seg.name, "")
        prev = self.baseline_meta.get(seg.name)
        if self.store is not None:
            version = self.store.save_baseline(
                self.config.node_id, seg.name, new,
                reason=reason, fingerprint=fp, captured_at=now, detail=detail,
            )
        else:
            version = (prev.version if prev else 0) + 1
        self.baselines[seg.name] = new
        self.trackers[seg.name] = PersistenceTracker()
        self.baseline_meta[seg.name] = BaselineMeta(version, now, str(reason), fp, dict(detail))
        self.rebaseline_deferred.pop(seg.name, None)
        self._drift_buf.pop(seg.name, None)
        self._emit(
            {
                "type": "baseline",
                "node_id": self.config.node_id,
                "segment": seg.name,
                "timestamp": now,
                "version": version,
                "reason": str(reason),
                "detail": detail,
            }
        )
        return True

    def _maybe_rebaseline(
        self, seg: SegmentConfig, now: float, spectrum: PowerSpectrum, findings: list[Finding]
    ) -> None:
        """Post-sweep policy check: pending deferral, max age, then drift."""
        if self._alert_grade(findings):
            self.gate.note_hit(seg.name, now)
        pending = self.rebaseline_deferred.get(seg.name)
        if pending is not None:
            self._recapture(seg, now, RecaptureReason(pending["reason"]), pending["detail"])
            return
        meta = self.baseline_meta.get(seg.name)
        base = self.baselines.get(seg.name)
        if meta is None:
            # Initial capture failed (device error at start): keep trying, or
            # the segment would run without a baseline for the process lifetime.
            self._recapture(seg, now, RecaptureReason.INITIAL, {})
            return
        if base is None:
            return
        trigger = stored_trigger(self.policy, meta, now, self.fingerprints.get(seg.name, ""))
        if trigger is not None:
            self._recapture(seg, now, *trigger)
            return
        if not self.policy.drift_window:
            return
        if self.gate.is_frozen(seg.name, now):
            self._drift_buf.pop(seg.name, None)  # never average incident sweeps
            return
        buf = self._drift_buf.setdefault(seg.name, [])
        buf.append(spectrum)
        if len(buf) < self.policy.drift_window:
            return
        report = drift_report(base, build_baseline(buf))
        self._drift_buf.pop(seg.name, None)
        if drift_exceeded(self.policy, report):
            self._recapture(
                seg, now, RecaptureReason.DRIFT,
                {"previous_version": meta.version, "drift": report},
            )

    def tick(self, now=None):
        now = self.clock() if now is None else now
        result = TickResult()
        for seg in self.scheduler.due(now, self.last_run):
            try:
                spectrum = next(iter(self.device.sweep(seg.plan, cycles=1)))
                base = self.baselines.get(seg.name)
                findings = (
                    sweep_findings(
                        spectrum,
                        base,
                        seg.plan,
                        k=self.config.threshold_k,
                        bands=bands_for(self.config.region),
                    )
                    if base
                    else []
                )
                if base:
                    findings = self.trackers[seg.name].update(findings)
                self.last_run[seg.name] = now
                result.segments_run.append(seg.name)
                result.findings += len(findings)
                event = {
                    "type": "sweep",
                    "node_id": self.config.node_id,
                    "site": self.config.site,
                    "floor": self.config.floor,
                    "room": self.config.room,
                    "segment": seg.name,
                    "timestamp": now,
                    "findings": [
                        {
                            "freq_hz": f.candidate.center_freq_hz,
                            "score": f.score,
                            "raw_score": f.raw_score,
                            "band": f.band_label,
                            "reasons": list(f.reasons),
                        }
                        for f in findings
                    ],
                }
                self.sink(event)
                if self.store is not None:
                    self.store.record_sweep(
                        self.config.node_id, seg.name, spectrum, event["findings"], timestamp=now
                    )
                if self.alert_engine is not None:
                    for alert in self.alert_engine.process(event):
                        for notify in self.notifiers:
                            notify(alert)
                if self.fleet is not None:
                    self.fleet.send_events(self.config.node_id, [event])
                    if now - self._last_heartbeat >= self.fleet_interval_s:
                        self._last_heartbeat = now
                        from quietroom.fleet import NodeIdentity, heartbeat_payload

                        ident = NodeIdentity(
                            self.config.node_id,
                            self.config.site,
                            self.config.floor,
                            self.config.room,
                        )
                        self.fleet.send_heartbeat(heartbeat_payload(ident, self.health(), now))
                self._maybe_rebaseline(seg, now, spectrum, findings)
            except Exception as e:  # keep other segments running; surface in health
                self.last_error = f"{seg.name}: {e}"
                result.errors.append(self.last_error)
        self.flush_alerts()
        return result

    def flush_alerts(self):
        """Deliver queued alerts (every tick, so retries happen with no new alert).

        Delivery problems are recorded per channel and never become sweep
        errors: a down SMTP relay must not look like a radio fault.
        """
        for ch in self.notifiers:
            flush = getattr(ch, "flush", None)
            if flush is None:
                continue
            name = getattr(ch, "name", repr(ch))
            try:
                flush()
                self.delivery_errors.pop(name, None)
            except Exception as e:  # outbox itself failed (disk, sqlite)
                self.delivery_errors[name] = f"{type(e).__name__}: {e}"

    def health(self):
        return {
            "node_id": self.config.node_id,
            "started_at": self.started_at,
            "baselines": sorted(self.baselines),
            "last_run": dict(self.last_run),
            "last_error": self.last_error,
            "baseline_versions": {
                name: {"version": m.version, "reason": m.reason, "captured_at": m.captured_at}
                for name, m in self.baseline_meta.items()
            },
            "rebaseline_deferred": dict(self.rebaseline_deferred),
            "alert_outbox": {
                ch.name: ch.pending() for ch in self.notifiers if hasattr(ch, "pending")
            },
            "alert_delivery_errors": dict(self.delivery_errors),
            "device": getattr(self, "device_info", None).__dict__
            if getattr(self, "device_info", None)
            else None,
        }

    def run(self, stop_event=None, max_ticks=None):
        self.start()
        if self.notifier is not None:
            self.notifier.ready()
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            if stop_event is not None and stop_event.is_set():
                break
            self.tick()
            ticks += 1
            if self.notifier is not None:
                self.notifier.watchdog()
            if stop_event is not None:
                stop_event.wait(self.config.poll_s)
            elif max_ticks is not None and ticks < max_ticks:
                time.sleep(min(self.config.poll_s, 0.05))
        return ticks
