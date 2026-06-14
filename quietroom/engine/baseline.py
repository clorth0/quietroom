"""Build a clean RF baseline and compare a live sweep against it."""
from __future__ import annotations

import numpy as np

from quietroom.engine.spectrum import Baseline, PowerSpectrum

# Floor on per-bin std (dB) so dead-quiet bins do not produce huge z-scores
# from a tiny denominator.
STD_FLOOR_DB = 2.0


def build_baseline(spectra: list[PowerSpectrum]) -> Baseline:
    """Average several sweeps into a per-bin mean and standard deviation."""
    if not spectra:
        raise ValueError("need at least one spectrum to build a baseline")
    freqs = spectra[0].freqs_hz
    for s in spectra:
        if not np.array_equal(s.freqs_hz, freqs):
            raise ValueError("all spectra must share the same frequency bins")
    stack = np.vstack([s.power_dbm for s in spectra])
    return Baseline(
        freqs_hz=freqs,
        mean_dbm=stack.mean(axis=0),
        std_dbm=stack.std(axis=0),
        sweep_count=len(spectra),
        created_at=spectra[-1].timestamp,
    )


def zscores(live: PowerSpectrum, baseline: Baseline) -> np.ndarray:
    """Per-bin standard-score of live power above the baseline mean."""
    if not np.array_equal(live.freqs_hz, baseline.freqs_hz):
        raise ValueError("live and baseline must share frequency bins")
    std = np.maximum(baseline.std_dbm, STD_FLOOR_DB)
    return (live.power_dbm - baseline.mean_dbm) / std
