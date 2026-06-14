"""Pure DSP for the audio-correlation test (no hardware imports)."""
from __future__ import annotations

import numpy as np


def make_chirp(duration_s: float, fs: int, f0: float = 500.0, f1: float = 3000.0) -> np.ndarray:
    """A linear frequency sweep, normalized to [-1, 1]; easy to cross-correlate."""
    n = int(duration_s * fs)
    t = np.arange(n) / fs
    k = (f1 - f0) / duration_s if duration_s > 0 else 0.0
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    return np.sin(phase).astype(np.float32)
