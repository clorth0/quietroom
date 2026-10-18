"""Hardware-independent scan state for the web layer."""
from __future__ import annotations

import threading
from typing import TypedDict

from quietroom.engine.persistence import PersistenceTracker
from quietroom.bands import bands_for
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.engine.score import band_label_for
from quietroom.engine.spectrum import Baseline, Candidate, Finding, PowerSpectrum
from quietroom.radio.device import Device
from quietroom.scan import capture_baseline as _capture_baseline
from quietroom.scan import investigate as _investigate


class SpectrumPayload(TypedDict):
    """Spectrum shape ScanSession emits to the browser (JSON: plain dict)."""

    f0: float
    f1: float
    powers: list[float]


class FindingPayload(TypedDict):
    """Finding shape ScanSession emits to the browser (JSON: plain dict)."""

    freq_hz: float
    freq_mhz: float
    score: float
    raw_score: float
    band: str
    reasons: list[str]


def spectrum_payload(ps: PowerSpectrum) -> SpectrumPayload:
    return {
        "f0": float(ps.freqs_hz[0]),
        "f1": float(ps.freqs_hz[-1]),
        "powers": [float(p) for p in ps.power_dbm],
    }


def finding_payload(f: Finding) -> FindingPayload:
    return {
        "freq_hz": float(f.candidate.center_freq_hz),
        "freq_mhz": round(f.candidate.center_freq_hz / 1e6, 3),
        "score": round(f.score, 1),
        "raw_score": round(f.raw_score, 1),
        "band": f.band_label,
        "reasons": list(f.reasons),
    }


class ScanSession:
    def __init__(
        self,
        device: Device,
        f_start_hz: int | None = None,
        f_stop_hz: int | None = None,
        bin_hz: int | None = None,
        region: str = "us",
        overview: bool = False,
    ) -> None:
        defaults = SweepPlan.search()
        start = defaults.start_hz if f_start_hz is None else f_start_hz
        stop = defaults.stop_hz if f_stop_hz is None else f_stop_hz
        width = defaults.bin_hz if bin_hz is None else bin_hz
        self.plan = (SweepPlan.overview if overview else SweepPlan.search)(
            start, stop, width,
        )
        self.device = device
        self.set_region(region)
        self.baseline: Baseline | None = None
        self._last: dict[float, Finding] = {}
        self._lock = threading.Lock()
        self.tracker = PersistenceTracker(window=5, min_hits=2)

    def set_region(self, region: str) -> None:
        """Switch the band catalog used to explain known emitters."""
        bands = bands_for(region)          # raises ValueError if unknown
        self.region = region.lower()
        self.bands = bands

    def score_label(self, c: Candidate) -> str:
        return band_label_for(c.center_freq_hz, self.bands)

    def capture_baseline(self, cycles: int = 5) -> int:
        baseline = _capture_baseline(self.device, self.plan, cycles=cycles)
        with self._lock:
            self.baseline = baseline
            self.tracker.reset()
        return baseline.sweep_count

    def sweep_once(self) -> tuple[SpectrumPayload, list[FindingPayload]]:
        spectrum = next(
            iter(self.device.sweep(self.plan, cycles=1))
        )
        with self._lock:
            baseline = self.baseline
        findings: list[Finding] = (
            sweep_findings(spectrum, baseline, self.plan, bands=self.bands)
            if baseline is not None else []
        )
        with self._lock:
            if baseline is not None:
                findings = self.tracker.update(findings)
                findings.sort(key=lambda f: f.score, reverse=True)
            self._last = {f.candidate.center_freq_hz: f for f in findings}
        return spectrum_payload(spectrum), [finding_payload(f) for f in findings]

    def investigate(self, freq_hz: float, **audio_kwargs) -> FindingPayload:
        with self._lock:
            finding = self._last.get(float(freq_hz))
            # Read the annotation facts from the recorded sweeps; the
            # tracker is not advanced -- investigating is not a sweep.
            persistence = (
                self.tracker.count_for(finding) if finding is not None else None
            )
        if finding is None:
            raise KeyError(f"no current suspect at {freq_hz} Hz")
        updated = _investigate(
            self.device, finding.candidate, bands=self.bands,
            persistence=persistence, **audio_kwargs,
        )
        with self._lock:
            self._last[float(freq_hz)] = updated
        return finding_payload(updated)
