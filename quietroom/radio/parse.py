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
