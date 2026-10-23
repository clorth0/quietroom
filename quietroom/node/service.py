"""Headless monitoring service: baselines per segment, scheduled sweeps, events.

Events are plain dicts delivered to a sink callable (and to the Store, alert
engine and fleet client when configured). Baselines are versioned in the Store
and refreshed under quietroom.node.rebaseline's policy; learning is frozen
while a segment has an active incident, and a refresh that diverges from the
segment's anchor baseline is held like one. Operator commands queued in the
Store (rebaseline / ack / promote-anchor) are applied at the start of a tick.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from quietroom.bands import bands_for
from quietroom.config import SegmentConfig
from quietroom.engine.baseline import build_baseline, drift_report
from quietroom.engine.persistence import PersistenceTracker
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum
from quietroom.node.rebaseline import (
    IncidentGate,
    RecaptureReason,
    anchor_divergence,
    drift_exceeded,
    frontend_fingerprint,
    stored_trigger,
)
from quietroom.node.scheduler import BandScheduler
from quietroom.radio.device import DeviceError
from quietroom.store import BaselineMeta


class FleetUplink(Protocol):
    """What the service needs from a fleet client (quietroom.fleet.FleetClient)."""

    def send_events(self, node_id: str, events: list[dict[str, Any]]) -> bool: ...
    def send_heartbeat(self, payload: dict[str, Any]) -> bool: ...
    def flush(self) -> int: ...
    def pending(self) -> int: ...


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
        fleet: FleetUplink | None = None,
        fleet_interval_s=60.0,
        notifier=None,
        clock=None,
        fleet_warnings=(),
    ) -> None:
        self.config = config
        self.device = device
        self.sink = sink or (lambda e: None)
        self.store = store
        self.alert_engine = alert_engine
        self.notifiers = list(notifiers)
        self.fleet = fleet
        self.fleet_interval_s = fleet_interval_s
        self.fleet_warnings = list(fleet_warnings)
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
        # anchor per segment for the current front end: version/set_at/reason/
        # fingerprint plus the loaded Baseline; anchor_status is the current
        # baseline (and any held candidate) compared against it.
        self.anchors: dict[str, dict[str, Any]] = {}
        self.anchor_status: dict[str, dict[str, Any]] = {}
        self._refusal: str | None = None

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
                    self._restore_holds(seg.name, now)
                    meta = self.store.baseline_meta(self.config.node_id, seg.name)
                self._load_anchor(seg.name, fp, now)
                if meta is None:
                    self._recapture(seg, now, RecaptureReason.INITIAL, {})
                    continue
                self.baselines[seg.name] = self.store.load_baseline(self.config.node_id, seg.name)
                self.trackers[seg.name] = PersistenceTracker()
                self.baseline_meta[seg.name] = meta
                if meta.fingerprint == fp:
                    div = self._check_anchor(seg, self.baselines[seg.name], now)
                    for f in div:  # e.g. a forced re-baseline not yet promoted
                        self.gate.note_hit(
                            seg.name, now, f.candidate.center_freq_hz, source="anchor"
                        )
                trigger = stored_trigger(self.policy, meta, now, fp)
                if trigger is not None:
                    self._recapture(seg, now, *trigger)
            except Exception as e:
                self.last_error = f"baseline {seg.name}: {e}"

    def _restore_holds(self, segment: str, now: float) -> None:
        """Rebuild the incident gate from stored findings minus applied acks."""
        assert self.store is not None
        since = now - self.policy.incident_hold_s
        node = self.config.node_id
        for ts, freq in self.store.incident_hits(node, segment, self.policy.freeze_score, since):
            self.gate.note_hit(segment, ts, freq)
        for at, freq in self.store.applied_acks(node, segment, since):
            self.gate.release(segment, at, freq)

    def _load_anchor(self, segment: str, fp: str, now: float) -> None:
        """Anchor for this front end; an upgraded node adopts its oldest baseline."""
        if self.store is None:
            if self.anchors.get(segment, {}).get("fingerprint") != fp:
                self.anchors.pop(segment, None)
            return
        node = self.config.node_id
        a = self.store.anchor(node, segment, fp)
        if a is None:
            first = self.store.earliest_version(node, segment, fp)
            if first is None:
                self.anchors.pop(segment, None)
                return
            self.store.set_anchor(
                node, segment, fp, first, set_at=now,
                reason="migrated: earliest stored baseline for this front end",
            )
            a = self.store.anchor(node, segment, fp)
        self.anchors[segment] = {
            **a,
            "fingerprint": fp,
            "baseline": self.store.load_baseline(node, segment, a["version"]),
        }

    def _set_anchor(
        self, seg: SegmentConfig, version: int, base: Baseline, now: float, reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        fp = self.fingerprints.get(seg.name, "")
        if self.store is not None:
            self.store.set_anchor(
                self.config.node_id, seg.name, fp, version, set_at=now, reason=reason,
                detail=detail,
            )
        self.anchors[seg.name] = {
            "version": version, "set_at": now, "reason": reason, "detail": dict(detail or {}),
            "fingerprint": fp, "baseline": base,
        }

    def _check_anchor(self, seg: SegmentConfig, base: Baseline, now: float) -> list[Finding]:
        """Score `base` against the anchor; record the result as the anchor status."""
        anchor = self.anchors.get(seg.name)
        if anchor is None or anchor["baseline"] is None:
            return []
        ref = anchor["baseline"]
        div = anchor_divergence(
            base, ref, seg.plan, k=self.config.threshold_k,
            bands=bands_for(self.config.region), min_score=self.policy.freeze_score,
        )
        report = drift_report(ref, base)
        self.anchor_status[seg.name] = {
            "divergence": [_finding_dict(f) for f in div],
            "drift": report,
            "broadband_drift": drift_exceeded(self.policy, report),
            "checked_at": now,
        }
        return div

    def _alert(self, event: dict[str, Any]) -> None:
        if self.alert_engine is not None:
            for alert in self.alert_engine.process(event):
                for notify in self.notifiers:
                    notify(alert)

    def _emit_divergence(
        self, seg: SegmentConfig, now: float, div: list[Finding], reason: RecaptureReason,
        adopted: bool,
    ) -> None:
        event = {
            "type": "anchor_divergence",
            "node_id": self.config.node_id,
            "site": self.config.site,
            "floor": self.config.floor,
            "room": self.config.room,
            "segment": seg.name,
            "timestamp": now,
            "anchor_version": self.anchors[seg.name]["version"],
            "candidate_reason": str(reason),
            "adopted": adopted,
            "findings": [_finding_dict(f) for f in div],
        }
        self._emit(event)
        self._alert(event)

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
        self,
        seg: SegmentConfig,
        now: float,
        reason: RecaptureReason,
        detail: dict[str, Any],
        *,
        force: bool = False,
        defer: bool = True,
    ) -> bool:
        """Capture and version a new baseline, unless learning is frozen.

        Returns True if a new baseline was adopted; otherwise ``self._refusal``
        says why. The guards are the module invariant: no capture while an
        incident holds the segment, a capture whose own sweeps show an
        alert-grade emitter (scored against the baseline it would replace) is
        discarded rather than learned, and so is one that diverges from the
        anchor. ``force`` (operator --force only) skips the guards but records
        what it learned over, and anchor divergence is still flagged and held.
        """
        self._refusal = None
        if not force and self.gate.is_frozen(seg.name, now):
            if reason is RecaptureReason.FINGERPRINT:
                # A baseline from another front end is not evidence of anything;
                # stop scoring against it rather than raise false incidents.
                self.baselines.pop(seg.name, None)
                self.trackers.pop(seg.name, None)
            until = self.gate.frozen_until(seg.name) or now
            why = f"incident active; learning frozen until {_utc(until)} ({until - now:.0f} s)"
            return self._refuse(seg, reason, detail, why, defer)
        old = None if reason is RecaptureReason.FINGERPRINT else self.baselines.get(seg.name)
        spectra = list(self.device.sweep(seg.plan, cycles=self.config.baseline_cycles))
        if not spectra:
            raise DeviceError(f"no baseline sweeps for {seg.name}")
        learned_over: list[float] = []
        if old is not None:
            for s in spectra:
                hits = self._alert_grade(
                    sweep_findings(
                        s, old, seg.plan, k=self.config.threshold_k,
                        bands=bands_for(self.config.region),
                    )
                )
                for h in hits:
                    self.gate.note_hit(seg.name, now, h.candidate.center_freq_hz)
                    learned_over.append(h.candidate.center_freq_hz)
                if hits and not force:
                    mhz = hits[0].candidate.center_freq_hz / 1e6
                    return self._refuse(
                        seg, reason, detail,
                        f"incident during capture: alert-grade emitter at {mhz:.3f} MHz", defer,
                    )
        new = build_baseline(spectra)
        anchor = self.anchors.get(seg.name)
        div = self._check_anchor(seg, new, now) if anchor is not None else []
        if div:
            for f in div:
                self.gate.note_hit(seg.name, now, f.candidate.center_freq_hz, source="anchor")
            self._emit_divergence(seg, now, div, reason, adopted=force)
            if not force:
                assert anchor is not None
                where = ", ".join(f"{f.candidate.center_freq_hz / 1e6:.3f}" for f in div)
                held = [_finding_dict(f) for f in div]
                cur = self.baselines.get(seg.name)
                if cur is not None:  # the status describes the baseline in use...
                    self._check_anchor(seg, cur, now)
                else:
                    self.anchor_status.pop(seg.name, None)
                # ...plus the candidate that was not learned
                self.anchor_status.setdefault(seg.name, {})["held_candidate"] = held
                return self._refuse(
                    seg, reason, detail,
                    f"candidate diverges from anchor v{anchor['version']}: alert-grade "
                    f"emitter at {where} MHz (promote the anchor if this change is legitimate)",
                    defer,
                )
        if force:
            detail = {
                **detail,
                "alert_grade_at_capture_mhz": sorted({round(f / 1e6, 4) for f in learned_over}),
                "anchor_divergence_mhz": [
                    round(f.candidate.center_freq_hz / 1e6, 4) for f in div
                ],
            }
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
        if anchor is None:
            self._set_anchor(seg, version, new, now, str(reason))
            self._check_anchor(seg, new, now)
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

    def _refuse(
        self, seg: SegmentConfig, reason: RecaptureReason, detail: dict[str, Any], why: str,
        defer: bool,
    ) -> bool:
        if defer:
            self._defer(seg, reason, detail, why)
        self._refusal = why
        return False

    def _maybe_rebaseline(
        self, seg: SegmentConfig, now: float, spectrum: PowerSpectrum, findings: list[Finding]
    ) -> None:
        """Post-sweep policy check: pending deferral, max age, then drift."""
        for f in self._alert_grade(findings):
            self.gate.note_hit(seg.name, now, f.candidate.center_freq_hz)
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
        if self.fleet is not None:
            # Retry the store-and-forward backlog every tick, even when no segment is due.
            self.fleet.flush()
        try:
            self.process_controls(now)
        except Exception as e:  # store trouble must not stop monitoring
            self.last_error = f"control requests: {e}"
            result.errors.append(self.last_error)
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
                    "findings": [_finding_dict(f) for f in findings],
                }
                self.sink(event)
                if self.store is not None:
                    self.store.record_sweep(
                        self.config.node_id, seg.name, spectrum, event["findings"], timestamp=now
                    )
                self._alert(event)
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

    # --- operator control requests -------------------------------------------

    def process_controls(self, now: float) -> None:
        """Apply operator commands queued in the Store (see quietroom-node --help)."""
        if self.store is None:
            return
        for req in self.store.pending_controls(self.config.node_id):
            try:
                status, result = self._apply_control(req, now)
            except Exception as e:  # device trouble during a manual capture
                status, result = "failed", {"why": f"{type(e).__name__}: {e}"}
            self.store.finish_control(req["id"], status, result, now=now)
            self._emit(
                {
                    "type": "control",
                    "node_id": self.config.node_id,
                    "segment": req["segment"],
                    "timestamp": now,
                    "request_id": req["id"],
                    "command": req["command"],
                    "args": req["args"],
                    "status": status,
                    "result": result,
                }
            )

    def _apply_control(self, req: dict[str, Any], now: float) -> tuple[str, dict[str, Any]]:
        seg = next(
            (s for s in self.config.segments if s.name == req["segment"] and s.enabled), None
        )
        if seg is None:
            return "failed", {"why": f"unknown or disabled segment {req['segment']!r}"}
        args = req["args"]
        note = str(args.get("note", ""))
        if req["command"] == "rebaseline":
            return self._manual_rebaseline(seg, req, now, bool(args.get("force")), note)
        if req["command"] == "ack":
            freq = args.get("freq_hz")
            released = self.gate.release(
                seg.name, req["requested_at"], None if freq is None else float(freq)
            )
            if self.alert_engine is not None:
                for a in self.alert_engine.open_alerts():
                    if a.get("segment") == seg.name and (
                        freq is None or abs(a["freq_hz"] - float(freq)) <= 50e3
                    ):
                        a["state"] = "acknowledged"
            return "done", {
                "released_mhz": [None if f is None else round(f / 1e6, 4) for f in released],
                "still_holding": self.gate.holding(seg.name, now),
            }
        if req["command"] == "promote-anchor":
            base, meta = self.baselines.get(seg.name), self.baseline_meta.get(seg.name)
            if base is None or meta is None:
                return "refused", {"why": "no baseline in use for this segment to promote"}
            prev = self.anchors.get(seg.name, {}).get("version")
            # The emitters that separate this baseline from the old anchor are what
            # the operator accepts: their holds (live, capture, anchor) end too.
            accepted = sorted(
                {float(f["freq_hz"]) for f in self.anchor_status.get(seg.name, {}).get(
                    "divergence", [])}
            )
            self._set_anchor(
                seg, meta.version, base, now, "promoted",
                {"request_id": req["id"], "note": note, "previous_anchor": prev},
            )
            self._check_anchor(seg, base, now)
            at = float(req["requested_at"])
            released = self.gate.release(seg.name, now, source="anchor")
            for f in accepted:
                released += self.gate.release(seg.name, at, f)
            return "done", {
                "version": meta.version,
                "previous_anchor": prev,
                "accepted_hz": accepted,
                "accepted_mhz": [round(f / 1e6, 4) for f in accepted],
                "released_mhz": sorted(
                    {round(f / 1e6, 4) for f in released if f is not None}
                ),
            }
        return "failed", {"why": f"unknown command {req['command']!r}"}

    def _manual_rebaseline(
        self, seg: SegmentConfig, req: dict[str, Any], now: float, force: bool, note: str
    ) -> tuple[str, dict[str, Any]]:
        meta = self.baseline_meta.get(seg.name)
        detail: dict[str, Any] = {
            "request_id": req["id"],
            "note": note,
            "forced": force,
            "previous_version": meta.version if meta else None,
        }
        if force:
            frozen = self.gate.is_frozen(seg.name, now)
            detail["frozen_until"] = self.gate.frozen_until(seg.name) if frozen else None
        if self._recapture(
            seg, now, RecaptureReason.MANUAL, detail, force=force, defer=False
        ):
            new = self.baseline_meta[seg.name]
            return "done", {
                "version": new.version,
                "forced": force,
                "anchor_divergence_mhz": new.detail.get("anchor_divergence_mhz", []),
            }
        why = self._refusal or "refused"
        if "frozen" in why or "incident" in why:
            why += "; acknowledge it once resolved (--ack) or override with --force"
        return "refused", {"why": why}

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
                continue
            parked = getattr(ch, "last_parked", None)
            if parked:
                # Through sink + fleet only: the channel that parked may be the
                # only one there is, and its trouble is what this reports.
                self._emit(
                    {
                        "type": "alert_parked",
                        "node_id": self.config.node_id,
                        "timestamp": self.clock(),
                        "channel": name,
                        "parked": parked,
                        "hint": "inspect: quietroom-node --outbox list --parked; "
                        "retry: quietroom-node --outbox requeue --id N",
                    }
                )

    def _fleet_health(self) -> dict:
        from quietroom.fleet import CERT_EXPIRY_WARNING_PREFIXES, cert_expiry_warning

        assert self.fleet is not None

        cert_status = getattr(self.fleet, "cert_status", None)
        cert = cert_status() if cert_status is not None else None
        warnings = [
            w for w in self.fleet_warnings if not w.startswith(CERT_EXPIRY_WARNING_PREFIXES)
        ]
        # Expiry is re-evaluated on every call: a long-running daemon must start
        # warning as the deadline approaches, not only if it was close at startup.
        expiry = cert_expiry_warning(cert) if cert else None
        if expiry:
            warnings.append(expiry)
        return {
            "fleet_auth": getattr(self.fleet, "auth_mode", None),
            "fleet_pending": self.fleet.pending(),
            "fleet_warnings": warnings,
            "fleet_cert": cert,
        }

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
            "anchors": {
                name: {
                    **{k: a[k] for k in ("version", "set_at", "reason")},
                    "divergence": [],
                    **self.anchor_status.get(name, {}),
                }
                for name, a in self.anchors.items()
            },
            "incident_hold": {
                name: {"frozen_until": self.gate.frozen_until(name), "emitters": held}
                for name in sorted(self.baselines.keys() | self.rebaseline_deferred.keys())
                if (held := self.gate.holding(name, self.clock()))
            },
            "alert_outbox": {
                ch.name: ch.pending() for ch in self.notifiers if hasattr(ch, "pending")
            },
            "alert_parked": {
                ch.name: ch.parked() for ch in self.notifiers if hasattr(ch, "parked")
            },
            "alert_delivery_errors": dict(self.delivery_errors),
            **(self._fleet_health() if self.fleet is not None else {}),
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
            elif max_ticks is None:
                time.sleep(self.config.poll_s)  # the daemon: never spin between ticks
            elif ticks < max_ticks:
                time.sleep(min(self.config.poll_s, 0.05))
        return ticks


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "freq_hz": f.candidate.center_freq_hz,
        "score": f.score,
        "raw_score": f.raw_score,
        "band": f.band_label,
        "reasons": list(f.reasons),
    }
