"""When a node may re-learn its baseline, and why it did.

The invariant this module exists to protect: **a node never learns an active
incident into its baseline.** Re-baselining is the obvious attack on an
anomaly detector (plant the bug, wait for the room to be "re-learned"), so
every trigger -- age, drift, front-end change -- is subordinate to an incident
gate, and every new baseline is versioned in the Store with the reason it was
captured.

Pure decisions live here; MonitorService owns the radio and the Store.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from quietroom.engine.plan import SweepPlan
from quietroom.radio.device import Device, DeviceInfo
from quietroom.store import BaselineMeta

__all__ = [
    "BaselineMeta",
    "IncidentGate",
    "RebaselinePolicy",
    "RecaptureReason",
    "drift_exceeded",
    "frontend_fingerprint",
    "stored_trigger",
]


class RecaptureReason(StrEnum):
    INITIAL = "initial"
    MAX_AGE = "max_age"
    DRIFT = "drift"
    FINGERPRINT = "fingerprint_changed"


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


class IncidentGate:
    """Per-segment 'learning frozen until' clock, fed by alert-grade findings."""

    def __init__(self, hold_s: float) -> None:
        self.hold_s = hold_s
        self._last_hit: dict[str, float] = {}

    def note_hit(self, segment: str, when: float) -> None:
        self._last_hit[segment] = max(when, self._last_hit.get(segment, when))

    def frozen_until(self, segment: str) -> float | None:
        last = self._last_hit.get(segment)
        return None if last is None else last + self.hold_s

    def is_frozen(self, segment: str, now: float) -> bool:
        until = self.frozen_until(segment)
        return until is not None and now < until
