"""One home for spectrum-grid knowledge: spacing, uniformity and alignment.

A module (in the codebase-design sense) with a small interface:
``SpectrumGrid.from_freqs(freqs)`` plus ``spacing_hz``, ``require_bin_hz``
and ``align``. Pipeline, baseline and the diff detector consume these
facts through this seam instead of re-deriving spacing or alignment, so a
grid change has locality here alone. Grid *construction* stays with the
adapters that produce spectra (radio/recorded, radio/parse, hackrf): they
translate a SweepPlan or a wire format into freqs; this module owns what
those freqs mean as a grid. See docs/agents/domain.md vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from quietroom.engine.spectrum import PowerSpectrum

# A sweep must cover at least this fraction of the grid to be comparable;
# below it the grids are different sweeps, not a dropped segment. Settled
# semantics (baseline MIN_OVERLAP, #14): preserved exactly.
MIN_OVERLAP = 0.5

# Tolerance (Hz) within which consecutive steps count as uniform, and a
# spacing counts as matching a requested bin. Preserves pipeline's 1 Hz.
_UNIFORM_TOL_HZ = 1.0


def spacing_hz(freqs_hz: np.ndarray) -> float:
    """The single spacing derivation: uniform step of `freqs_hz`.

    Raises ValueError for fewer than two bins or non-uniform spacing,
    with the messages pipeline previously owned.
    """
    freqs = np.asarray(freqs_hz, dtype=float)
    if len(freqs) < 2:
        raise ValueError("spectrum needs at least two bins to check against the plan")
    diffs = np.diff(freqs)
    step = float(diffs[0])
    if bool(np.any(np.abs(diffs - step) > _UNIFORM_TOL_HZ)):
        raise ValueError("spectrum bin spacing is not uniform")
    return step


@dataclass(frozen=True, eq=False)
class SpectrumGrid:
    """A frequency grid: the freqs a spectrum is on, plus their facts."""

    freqs_hz: np.ndarray

    @classmethod
    def from_freqs(cls, freqs_hz: np.ndarray) -> SpectrumGrid:
        return cls(np.asarray(freqs_hz, dtype=float))

    def __len__(self) -> int:
        return len(self.freqs_hz)

    @property
    def spacing_hz(self) -> float:
        return spacing_hz(self.freqs_hz)

    def require_bin_hz(self, bin_hz: float) -> None:
        """Raise unless this grid's spacing matches `bin_hz` (plan check)."""
        spacing = self.spacing_hz
        if abs(spacing - bin_hz) > _UNIFORM_TOL_HZ:
            raise ValueError(
                f"spectrum bin spacing {spacing:.0f} Hz does not match "
                f"the plan bin {bin_hz} Hz"
            )

    def matches(self, freqs_hz: np.ndarray) -> bool:
        return bool(np.array_equal(self.freqs_hz, freqs_hz))

    def align(self, ps: PowerSpectrum) -> np.ndarray:
        """Snap a spectrum onto this grid (bins match within half a step).

        Grid bins with no matching sample are NaN. Raises ValueError when
        fewer than MIN_OVERLAP of the grid bins are covered. The source
        spectrum need not be uniform (a dropped segment is exactly that);
        For a uniform grid its spacing sets the tolerance. A grid with
        a dropped interior bin is not uniform; it keeps the historical
        fallback tolerance (half the minimum step) so a segment dropped
        from every baseline sweep still aligns instead of aborting.
        """
        grid = self.freqs_hz
        out = np.full(len(grid), np.nan)
        if len(ps.freqs_hz) == 0 or len(grid) == 0:
            raise ValueError("cannot align an empty spectrum or grid")
        if len(grid) > 1:
            try:
                step = self.spacing_hz
            except ValueError:
                step = float(np.min(np.diff(grid)))  # dropped-bin grid
        else:
            step = 1.0
        tol = step / 2.0
        idx = np.searchsorted(grid, ps.freqs_hz)
        for k, (f, p) in enumerate(zip(ps.freqs_hz, ps.power_dbm, strict=True)):
            for j in (idx[k] - 1, idx[k]):
                if 0 <= j < len(grid) and abs(grid[j] - f) < tol:
                    out[j] = p
                    break
        if np.count_nonzero(~np.isnan(out)) < MIN_OVERLAP * len(grid):
            raise ValueError("spectra do not share enough frequency bins to compare")
        return out
