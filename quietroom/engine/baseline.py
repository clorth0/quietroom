"""Build a clean RF baseline and compare a live sweep against it."""
from __future__ import annotations

import warnings

import numpy as np

from quietroom.engine.grid import MIN_OVERLAP, SpectrumGrid
from quietroom.engine.spectrum import Baseline, PowerSpectrum

# Floor on per-bin std (dB) so dead-quiet bins do not produce huge z-scores
# from a tiny denominator.
STD_FLOOR_DB = 2.0
# Power used for a grid bin that no baseline sweep ever filled.
DEAD_BIN_DBM = -120.0
# Re-exported for compatibility; the constant's home is engine.grid.
__all__ = ["DEAD_BIN_DBM", "MIN_OVERLAP", "STD_FLOOR_DB", "align_to",
           "aligned_live", "build_baseline", "zscores", "zscores_aligned"]


def align_to(ps: PowerSpectrum, grid: np.ndarray) -> np.ndarray:
    """Snap a spectrum onto `grid`; semantics owned by SpectrumGrid.align."""
    return SpectrumGrid.from_freqs(grid).align(ps)


def build_baseline(spectra: list[PowerSpectrum]) -> Baseline:
    """Average several sweeps into a per-bin mean and standard deviation.

    Sweeps are aligned onto the most complete sweep's grid, so a segment
    dropped from one sweep does not abort the baseline.
    """
    if not spectra:
        raise ValueError("need at least one spectrum to build a baseline")
    grid = max(spectra, key=lambda s: len(s.freqs_hz)).grid
    freqs = grid.freqs_hz
    stack = np.vstack([grid.align(s) for s in spectra])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN columns
        mean = np.nanmean(stack, axis=0)
        std = np.nanstd(stack, axis=0)
    mean = np.where(np.isnan(mean), DEAD_BIN_DBM, mean)
    std = np.where(np.isnan(std), 0.0, std)
    return Baseline(
        freqs_hz=freqs,
        mean_dbm=mean,
        std_dbm=std,
        sweep_count=len(spectra),
        created_at=spectra[-1].timestamp,
    )


def aligned_live(live: PowerSpectrum, baseline: Baseline) -> PowerSpectrum:
    """The live sweep on the baseline grid; missing bins take the baseline mean."""
    if baseline.grid.matches(live.freqs_hz):
        return live
    power = baseline.grid.align(live)
    power = np.where(np.isnan(power), baseline.mean_dbm, power)
    return PowerSpectrum(baseline.freqs_hz, power, live.timestamp)


def zscores_aligned(live: PowerSpectrum, baseline: Baseline) -> np.ndarray:
    """Z-scores for a live spectrum already on the baseline grid.

    The caller owns the (single) alignment; this avoids re-aligning a
    spectrum find_candidates has already snapped to the grid. Alignment
    is idempotent, so results are identical to zscores on the raw live.
    """
    std = np.maximum(baseline.std_dbm, STD_FLOOR_DB)
    return (live.power_dbm - baseline.mean_dbm) / std


def zscores(live: PowerSpectrum, baseline: Baseline) -> np.ndarray:
    """Per-bin standard-score of live power above the baseline mean."""
    return zscores_aligned(aligned_live(live, baseline), baseline)
