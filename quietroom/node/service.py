"""Headless monitoring service: baselines per segment, scheduled sweeps, events.

Events are plain dicts delivered to a sink callable; item 2/3 PRs replace the
in-memory sink with SQLite storage and alerting without changing this seam.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from quietroom.bands import bands_for
from quietroom.engine.baseline import build_baseline
from quietroom.engine.persistence import PersistenceTracker
from quietroom.engine.pipeline import sweep_findings
from quietroom.node.scheduler import BandScheduler
from quietroom.radio.device import DeviceError


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
    ):
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
        self.scheduler = BandScheduler(config.segments)
        self.baselines = {}
        self.trackers = {}
        self.last_run = {}
        self.last_error = None
        self.started_at = None

    def start(self):
        info = self.device.probe()
        if info is None:
            raise DeviceError("radio not detected")
        self.device_info = info
        self.started_at = time.time()
        self.capture_baselines()
        return info

    def capture_baselines(self):
        for seg in self.config.segments:
            if not seg.enabled:
                continue
            try:
                if self.store is not None:
                    stored = self.store.load_baseline(self.config.node_id, seg.name)
                    if stored is not None:
                        self.baselines[seg.name] = stored
                        self.trackers[seg.name] = PersistenceTracker()
                        continue
                spectra = list(self.device.sweep(seg.plan, cycles=self.config.baseline_cycles))
                if not spectra:
                    raise DeviceError(f"no baseline sweeps for {seg.name}")
                self.baselines[seg.name] = build_baseline(spectra)
                self.trackers[seg.name] = PersistenceTracker()
                if self.store is not None:
                    self.store.save_baseline(
                        self.config.node_id, seg.name, self.baselines[seg.name]
                    )
            except Exception as e:
                self.last_error = f"baseline {seg.name}: {e}"

    def tick(self, now=None):
        now = time.time() if now is None else now
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
                        self.config.node_id, seg.name, spectrum, event["findings"]
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
            except Exception as e:  # keep other segments running; surface in health
                self.last_error = f"{seg.name}: {e}"
                result.errors.append(self.last_error)
        return result

    def health(self):
        return {
            "node_id": self.config.node_id,
            "started_at": self.started_at,
            "baselines": sorted(self.baselines),
            "last_run": dict(self.last_run),
            "last_error": self.last_error,
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
