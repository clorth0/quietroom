"""Build a clean RF baseline and compare a live sweep against it."""
from __future__ import annotations

import warnings

import numpy as np

from quietroom.engine.spectrum import Baseline, PowerSpectrum

# Floor on per-bin std (dB) so dead-quiet bins do not produce huge z-scores
# from a tiny denominator.
STD_FLOOR_DB = 2.0
# Power used for a grid bin that no baseline sweep ever filled.
DEAD_BIN_DBM = -120.0
# A sweep must cover at least this fraction of the grid to be comparable;
# below it the grids are different sweeps, not a dropped segment.
MIN_OVERLAP = 0.5


def align_to(ps: PowerSpectrum, grid: np.ndarray) -> np.ndarray:
    """Snap a spectrum onto `grid` (bins match within half a grid step).

    Grid bins with no matching sample are NaN. Raises ValueError when fewer
    than MIN_OVERLAP of the grid bins are covered.
    """
    grid = np.asarray(grid, dtype=float)
    out = np.full(len(grid), np.nan)
    if len(ps.freqs_hz) == 0 or len(grid) == 0:
        raise ValueError("cannot align an empty spectrum or grid")
    step = float(np.min(np.diff(grid))) if len(grid) > 1 else 1.0
    tol = step / 2.0
    idx = np.searchsorted(grid, ps.freqs_hz)
    for k, (f, p) in enumerate(zip(ps.freqs_hz, ps.power_dbm)):
        for j in (idx[k] - 1, idx[k]):
            if 0 <= j < len(grid) and abs(grid[j] - f) < tol:
                out[j] = p
                break
    if np.count_nonzero(~np.isnan(out)) < MIN_OVERLAP * len(grid):
        raise ValueError("spectra do not share enough frequency bins to compare")
    return out


def build_baseline(spectra: list[PowerSpectrum]) -> Baseline:
    """Average several sweeps into a per-bin mean and standard deviation.

    Sweeps are aligned onto the most complete sweep's grid, so a segment
    dropped from one sweep does not abort the baseline.
    """
    if not spectra:
        raise ValueError("need at least one spectrum to build a baseline")
    freqs = max(spectra, key=lambda s: len(s.freqs_hz)).freqs_hz
    stack = np.vstack([align_to(s, freqs) for s in spectra])
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
    if np.array_equal(live.freqs_hz, baseline.freqs_hz):
        return live
    power = align_to(live, baseline.freqs_hz)
    power = np.where(np.isnan(power), baseline.mean_dbm, power)
    return PowerSpectrum(baseline.freqs_hz, power, live.timestamp)


def zscores(live: PowerSpectrum, baseline: Baseline) -> np.ndarray:
    """Per-bin standard-score of live power above the baseline mean."""
    live = aligned_live(live, baseline)
    std = np.maximum(baseline.std_dbm, STD_FLOOR_DB)
    return (live.power_dbm - baseline.mean_dbm) / std
