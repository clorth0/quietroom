"""Hardware-independent scan state for the web layer."""
from __future__ import annotations

import threading

from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.spectrum import Finding, PowerSpectrum
from quietroom.radio.device import Device
from quietroom.scan import capture_baseline as _capture_baseline
from quietroom.scan import investigate as _investigate


def spectrum_payload(ps: PowerSpectrum) -> dict:
    return {
        "f0": float(ps.freqs_hz[0]),
        "f1": float(ps.freqs_hz[-1]),
        "powers": [float(p) for p in ps.power_dbm],
    }


def finding_payload(f: Finding) -> dict:
    return {
        "freq_hz": float(f.candidate.center_freq_hz),
        "freq_mhz": round(f.candidate.center_freq_hz / 1e6, 3),
        "score": round(f.score, 1),
        "band": f.band_label,
        "reasons": list(f.reasons),
    }


class ScanSession:
    def __init__(
        self,
        device: Device,
        f_start_hz: int = 300_000_000,
        f_stop_hz: int = 320_000_000,
        bin_hz: int = 1_000_000,
    ) -> None:
        self.device = device
        self.f_start_hz = f_start_hz
        self.f_stop_hz = f_stop_hz
        self.bin_hz = bin_hz
        self.baseline = None
        self._last: dict[float, Finding] = {}
        self._lock = threading.Lock()

    def capture_baseline(self, cycles: int = 5) -> int:
        baseline = _capture_baseline(
            self.device, self.f_start_hz, self.f_stop_hz, self.bin_hz, cycles=cycles
        )
        with self._lock:
            self.baseline = baseline
        return baseline.sweep_count

    def sweep_once(self) -> tuple[dict, list[dict]]:
        spectrum = next(
            iter(self.device.sweep(self.f_start_hz, self.f_stop_hz, self.bin_hz, cycles=1))
        )
        with self._lock:
            baseline = self.baseline
        findings = sweep_findings(spectrum, baseline) if baseline is not None else []
        with self._lock:
            self._last = {f.candidate.center_freq_hz: f for f in findings}
        return spectrum_payload(spectrum), [finding_payload(f) for f in findings]

    def investigate(self, freq_hz: float, **audio_kwargs) -> dict:
        with self._lock:
            finding = self._last.get(float(freq_hz))
        if finding is None:
            raise KeyError(f"no current suspect at {freq_hz} Hz")
        updated = _investigate(self.device, finding.candidate, **audio_kwargs)
        with self._lock:
            self._last[float(freq_hz)] = updated
        return finding_payload(updated)
