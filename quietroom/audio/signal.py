"""Pure DSP for the audio-correlation test (no hardware imports)."""
from __future__ import annotations

import numpy as np
from scipy.signal import resample


def make_chirp(duration_s: float, fs: int, f0: float = 500.0, f1: float = 3000.0) -> np.ndarray:
    """A linear frequency sweep, normalized to [-1, 1]; easy to cross-correlate."""
    n = int(duration_s * fs)
    t = np.arange(n) / fs
    k = (f1 - f0) / duration_s if duration_s > 0 else 0.0
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    return np.sin(phase).astype(np.float32)


def resample_to(x: np.ndarray, num: int) -> np.ndarray:
    """Resample x to exactly `num` samples (FFT-based). num<=0 -> empty array."""
    if num <= 0:
        return np.zeros(0, dtype=float)
    return resample(np.asarray(x, dtype=float), num)


def iq_to_am_envelope(iq: np.ndarray, in_fs: int, out_fs: int) -> np.ndarray:
    """Amplitude (AM) envelope of an IQ stream, DC-blocked and resampled to out_fs."""
    mag = np.abs(np.asarray(iq))
    if len(mag) == 0:
        return np.zeros(0, dtype=float)
    mag = mag - mag.mean()                       # drop the carrier/DC level
    n_out = max(int(len(mag) * out_fs / in_fs), 1)
    return resample_to(mag, n_out)
