"""Pure parsing helpers for HackRF sweep CSV and cs8 IQ bytes."""
from __future__ import annotations

import numpy as np

from quietroom.engine.spectrum import PowerSpectrum

# Power assigned to any non-finite sweep bin (dBm noise floor).
DEAD_BIN_DBM = -120.0


def parse_sweep_line(line: str) -> tuple[int, float, list[float]]:
    """Parse one `hackrf_sweep` CSV line into (hz_low, bin_width_hz, powers_dbm)."""
    parts = line.strip().split(", ")
    hz_low = int(parts[2])
    bin_width = float(parts[4])
    powers = [float(p) for p in parts[6:]]
    return hz_low, bin_width, powers


class SweepAccumulator:
    """Accumulate `hackrf_sweep` segments into complete PowerSpectrum sweeps.

    Segments within one sweep cycle arrive in arbitrary frequency order. A
    repeated `hz_low` signals the start of the next cycle. `add()` returns a
    completed PowerSpectrum when a cycle boundary is crossed, else None.
    """

    def __init__(self) -> None:
        self._bins: dict[int, float] = {}
        self._seen: set[int] = set()

    def add(
        self, hz_low: int, bin_width: float, powers: list[float], timestamp: float
    ) -> PowerSpectrum | None:
        result = None
        if hz_low in self._seen:
            result = self._flush(timestamp)
        self._seen.add(hz_low)
        for i, p in enumerate(powers):
            freq = int(hz_low + (i + 0.5) * bin_width)
            self._bins[freq] = p
        return result

    def _flush(self, timestamp: float) -> PowerSpectrum | None:
        if not self._bins:
            return None
        freqs = np.array(sorted(self._bins), dtype=float)
        powers = np.array([self._bins[int(f)] for f in freqs], dtype=float)
        powers = np.where(np.isfinite(powers), powers, DEAD_BIN_DBM)
        self._bins = {}
        self._seen = set()
        return PowerSpectrum(freqs_hz=freqs, power_dbm=powers, timestamp=timestamp)


def iq_cs8_to_complex(raw: bytes) -> np.ndarray:
    """Convert interleaved signed-8-bit IQ bytes to a complex64 array in [-1, 1)."""
    a = np.frombuffer(raw, dtype=np.int8)
    n = (len(a) // 2) * 2
    a = a[:n].astype(np.float32) / 128.0
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)
