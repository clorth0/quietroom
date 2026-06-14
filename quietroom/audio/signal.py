"""Pure DSP for the audio-correlation test (no hardware imports)."""
from __future__ import annotations

import numpy as np
from scipy.signal import correlate, resample


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


def iq_to_fm_envelope(iq: np.ndarray, in_fs: int, out_fs: int) -> np.ndarray:
    """FM (phase-discriminator) envelope of an IQ stream, resampled to out_fs."""
    x = np.asarray(iq)
    if len(x) < 2:
        return np.zeros(0, dtype=float)
    disc = np.angle(x[1:] * np.conj(x[:-1]))     # instantaneous frequency
    disc = disc - disc.mean()
    # Size the output from the original IQ length (not len(disc), which is one
    # shorter) so AM and FM envelopes of the same capture come out equal-length.
    n_out = max(int(len(x) * out_fs / in_fs), 1)
    return resample_to(disc, n_out)


def best_lag_correlation(a: np.ndarray, b: np.ndarray, max_lag: int) -> float:
    """Peak normalized cross-correlation (0..1) of a and b over lags in +/-max_lag."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    denom = np.sqrt(np.sum(a * a) * np.sum(b * b))
    if denom == 0:
        return 0.0
    corr = correlate(a, b, mode="full")          # length 2n-1; zero lag at index n-1
    mid = n - 1
    lo = max(0, mid - max_lag)
    hi = min(len(corr), mid + max_lag + 1)
    peak = float(np.max(np.abs(corr[lo:hi])))
    return peak / denom
