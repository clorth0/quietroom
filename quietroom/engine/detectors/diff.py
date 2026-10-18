"""Baseline-differencing detector: turn excess-power bins into candidates."""
from __future__ import annotations

import numpy as np

from quietroom.engine.baseline import aligned_live, zscores_aligned
from quietroom.engine.spectrum import Baseline, Candidate, PowerSpectrum


def find_candidates(
    live: PowerSpectrum,
    baseline: Baseline,
    k: float = 4.0,
    min_bins: int = 1,
) -> list[Candidate]:
    """Group contiguous bins whose power exceeds baseline by > k sigma."""
    # One alignment per sweep: zscores_aligned trusts this aligned live
    # (the old second alignment in zscores was an idempotent no-op).
    live = aligned_live(live, baseline)
    z = zscores_aligned(live, baseline)
    mask = z > k
    n = len(mask)
    bin_hz = live.grid.spacing_hz if n > 1 else 0.0
    candidates: list[Candidate] = []

    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        if (j - i) >= min_bins:
            seg_freqs = live.freqs_hz[i:j]
            n_bins = j - i
            seg_power = live.power_dbm[i:j]
            peak = int(np.argmax(seg_power))
            # Each bin covers bin_hz, so the occupied width is n_bins * bin_hz
            # (last - first undercounts by one bin and is 0 for a single bin).
            bw = n_bins * bin_hz
            excess = float(seg_power[peak] - baseline.mean_dbm[i + peak])
            candidates.append(
                Candidate(
                    center_freq_hz=float(seg_freqs[peak]),
                    bandwidth_hz=bw,
                    peak_power_dbm=float(seg_power[peak]),
                    snr_over_baseline_db=excess,
                    bin_hz=bin_hz,
                    n_bins=n_bins,
                )
            )
        i = j
    return candidates
