"""When a node may re-learn its baseline, and why it did.

The invariant this module exists to protect: **a node never learns an active
incident into its baseline.** Re-baselining is the obvious attack on an
anomaly detector (plant the bug, wait for the room to be "re-learned"), so
every trigger -- age, drift, front-end change -- is subordinate to an incident
gate, and every new baseline is versioned in the Store with the reason it was
captured.

Refreshing opens a second, slower attack: walk the emitter up a few dB per
refresh period, so each live sweep sees only a small step over the *current*
baseline and each refresh learns the step. The **anchor** closes it: a
long-lived reference baseline per front end that refreshes never move. A
refresh candidate showing an alert-grade emitter against the anchor is held
exactly like an incident (and alerted), so cumulative creep cannot be
absorbed. Only an operator moves the anchor (``--promote-anchor``).

Pure decisions live here; MonitorService owns the radio and the Store.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from quietroom.bands import Band
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum
from quietroom.radio.device import Device, DeviceInfo
from quietroom.store import BaselineMeta

__all__ = [
    "BaselineMeta",
    "IncidentGate",
    "RebaselinePolicy",
    "RecaptureReason",
    "anchor_divergence",
    "drift_exceeded",
    "frontend_fingerprint",
    "stored_trigger",
]


class RecaptureReason(StrEnum):
    INITIAL = "initial"
    MAX_AGE = "max_age"
    DRIFT = "drift"
    FINGERPRINT = "fingerprint_changed"
    MANUAL = "manual"


@dataclass(frozen=True)
class RebaselinePolicy:
    """Refresh policy. Defaults reproduce the old behaviour: reuse forever.

    max_age_s: recapture once the stored baseline is older than this (None = never).
    drift_window: number of quiet live sweeps averaged into a drift candidate
        (0 = drift trigger off; >= 2 when on).
    drift_mean_db / drift_changed_fraction: drift_report thresholds; either trips.
    freeze_score: a finding at or above this score is an incident (defaults to
        the alert threshold in config, so "alerting" and "frozen" agree).
    incident_hold_s: learning stays frozen this long after the last incident hit.
    invalidate_on_fingerprint: recapture when plan/front end/antenna changed.
    """

    max_age_s: float | None = None
    drift_window: int = 0
    drift_mean_db: float = 3.0
    drift_changed_fraction: float = 0.10
    freeze_score: float = 80.0
    incident_hold_s: float = 3600.0
    invalidate_on_fingerprint: bool = True

    def __post_init__(self) -> None:
        age = self.max_age_s
        if age is not None and not (age > 0 and math.isfinite(age)):
            raise ValueError("baseline max_age_s must be a positive number of seconds")
        if self.drift_window < 0 or self.drift_window == 1:
            raise ValueError("baseline drift_window must be 0 (off) or >= 2 sweeps")
        if self.drift_mean_db <= 0:
            raise ValueError("baseline drift_mean_db must be > 0")
        if not 0 < self.drift_changed_fraction <= 1:
            raise ValueError("baseline drift_changed_fraction must be in (0, 1]")
        if self.incident_hold_s < 0:
            raise ValueError("baseline incident_hold_s must be >= 0")
        if self.freeze_score < 0:
            raise ValueError("baseline freeze_score must be >= 0")


def frontend_fingerprint(
    plan: SweepPlan, device: Device, info: DeviceInfo | None, antenna: str
) -> str:
    """Hash of everything that makes two baselines incomparable.

    Sweep grid, radio model + serial (units differ), front-end gains and the
    configured antenna. Region/threshold are scoring inputs, not RF ones, so
    they are deliberately excluded.
    """
    payload = {
        "plan": [plan.start_hz, plan.stop_hz, plan.bin_hz],
        "device": type(device).__name__,
        "board": info.board if info else "",
        "serial": info.serial if info else "",
        "frontend": device.frontend_settings(),
        "antenna": antenna,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def drift_exceeded(policy: RebaselinePolicy, report: dict[str, float]) -> bool:
    return (
        report["mean_delta_db"] >= policy.drift_mean_db
        or report["changed_fraction"] >= policy.drift_changed_fraction
    )


def stored_trigger(
    policy: RebaselinePolicy, meta: BaselineMeta, now: float, fingerprint: str
) -> tuple[RecaptureReason, dict[str, Any]] | None:
    """Does the stored baseline itself demand a recapture (age / fingerprint)?"""
    if policy.invalidate_on_fingerprint and meta.fingerprint != fingerprint:
        return RecaptureReason.FINGERPRINT, {
            "previous_version": meta.version,
            "previous_fingerprint": meta.fingerprint,
        }
    age = now - meta.captured_at
    if policy.max_age_s is not None and age >= policy.max_age_s:
        return RecaptureReason.MAX_AGE, {"previous_version": meta.version, "age_s": age}
    return None


def anchor_divergence(
    candidate: Baseline,
    anchor: Baseline,
    plan: SweepPlan,
    *,
    k: float,
    bands: list[Band],
    min_score: float,
) -> list[Finding]:
    """Alert-grade emitters in `candidate` that the anchor does not have.

    The candidate's mean is scored against the anchor with the live detector,
    so "diverged" means exactly what an alert means. Averaged spectra are
    quieter than single sweeps, so this is no noisier than live detection.
    """
    as_sweep = PowerSpectrum(candidate.freqs_hz, candidate.mean_dbm, candidate.created_at)
    found = sweep_findings(as_sweep, anchor, plan, k=k, bands=bands)
    return [f for f in found if f.score >= min_score]


CLUSTER_HZ = 25_000.0
ACK_TOLERANCE_HZ = 50_000.0


class IncidentGate:
    """Per-segment 'learning frozen until' clock, fed by alert-grade findings.

    Hits are kept per emitter (frequency cluster) and source ("live" sweeps or
    "anchor" divergence), so an operator acknowledging one emitter releases
    only that emitter: the segment stays frozen while any other is held.
    ``freq_hz=None`` is a segment-wide hit, released only by a segment-wide ack.
    """

    def __init__(self, hold_s: float, cluster_hz: float = CLUSTER_HZ) -> None:
        self.hold_s = hold_s
        self.cluster_hz = cluster_hz
        self._hits: dict[str, dict[tuple[str, int | None], float]] = {}

    def note_hit(
        self, segment: str, when: float, freq_hz: float | None = None, source: str = "live"
    ) -> None:
        key = (source, None if freq_hz is None else round(freq_hz / self.cluster_hz))
        hits = self._hits.setdefault(segment, {})
        hits[key] = max(when, hits.get(key, when))

    def release(
        self,
        segment: str,
        at: float,
        freq_hz: float | None = None,
        *,
        source: str | None = None,
        tolerance_hz: float = ACK_TOLERANCE_HZ,
    ) -> list[float | None]:
        """Forget hits up to `at` (never later ones); returns released frequencies."""
        hits = self._hits.get(segment, {})
        released: list[float | None] = []
        for (src, cl), last in list(hits.items()):
            if last > at or (source is not None and src != source):
                continue
            f = None if cl is None else cl * self.cluster_hz
            if freq_hz is not None and (f is None or abs(f - freq_hz) > tolerance_hz):
                continue
            del hits[(src, cl)]
            if f not in released:
                released.append(f)
        return released

    def frozen_until(self, segment: str) -> float | None:
        hits = self._hits.get(segment)
        return max(hits.values()) + self.hold_s if hits else None

    def is_frozen(self, segment: str, now: float) -> bool:
        until = self.frozen_until(segment)
        return until is not None and now < until

    def holding(self, segment: str, now: float) -> list[dict[str, Any]]:
        """Emitters still holding the segment at `now`, oldest cluster first."""
        return [
            {
                "freq_hz": None if cl is None else cl * self.cluster_hz,
                "source": src,
                "last_hit": last,
                "until": last + self.hold_s,
            }
            for (src, cl), last in sorted(
                self._hits.get(segment, {}).items(), key=lambda kv: (kv[0][1] or 0, kv[0][0])
            )
            if now < last + self.hold_s
        ]
