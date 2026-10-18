"""Characterization for candidate #4: spectrum grid knowledge has one home.

Pins current signal/grid outputs (aligned arrays, diff/baseline results on
known inputs, boundary/off-grid frequencies) and the one-home property:
spacing derivation and alignment live only in engine.grid, consumed by
pipeline, baseline and diff.
"""
import inspect

import numpy as np
import pytest

import quietroom.engine.baseline as baseline_mod
import quietroom.engine.detectors.diff as diff_mod
import quietroom.engine.pipeline as pipeline_mod
from quietroom.engine.baseline import aligned_live, build_baseline, zscores
from quietroom.engine.detectors.diff import find_candidates
from quietroom.engine.grid import SpectrumGrid
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.engine.spectrum import Baseline, PowerSpectrum


def _ps(freqs, power, t=0.0):
    return PowerSpectrum(np.array(freqs, dtype=float),
                         np.array(power, dtype=float), t)


# --- the grid module's interface -------------------------------------------

def test_grid_spacing_is_the_uniform_step():
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 102e6])
    assert grid.spacing_hz == 1e6
    assert len(grid) == 3
    np.testing.assert_array_equal(grid.freqs_hz, [100e6, 101e6, 102e6])


def test_grid_spacing_rejects_fewer_than_two_bins():
    with pytest.raises(ValueError, match="at least two bins"):
        _ = SpectrumGrid.from_freqs([100e6]).spacing_hz


def test_grid_spacing_rejects_non_uniform_freqs():
    with pytest.raises(ValueError, match="not uniform"):
        _ = SpectrumGrid.from_freqs([100e6, 101e6, 103e6]).spacing_hz


def test_grid_require_bin_hz_preserves_pipeline_message():
    grid = SpectrumGrid.from_freqs([100e6, 102e6])
    grid.require_bin_hz(2e6)  # matches: no raise
    with pytest.raises(ValueError, match="does not match"):
        grid.require_bin_hz(25_000)


def test_spectrum_and_baseline_expose_their_grid():
    ps = _ps([100e6, 101e6], [-90, -90])
    assert ps.grid.spacing_hz == 1e6
    base = Baseline(ps.freqs_hz, np.array([-90., -90.]),
                    np.array([0., 0.]), 1, 0.0)
    assert base.grid.spacing_hz == 1e6


# --- alignment semantics (formerly baseline.align_to) -----------------------

def test_align_snaps_within_half_a_step_and_marks_missing_nan():
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 102e6, 103e6])
    out = grid.align(_ps([100e6, 101.4e6, 103e6], [-90, -50, -80]))
    assert out[0] == -90 and out[1] == -50 and out[3] == -80
    assert np.isnan(out[2])


def test_align_at_exactly_half_a_step_does_not_snap():
    # Current semantics: match is strict (< tol), half-step is off-grid.
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 102e6, 103e6])
    out = grid.align(_ps([100e6, 101e6, 102e6, 100.5e6],
                         [-90, -90, -90, -40]))
    assert out[0] == -90  # the -40 at exactly half-step overwrote nothing
    assert not np.any(out == -40)


def test_align_just_inside_half_a_step_snaps():
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 102e6, 103e6])
    out = grid.align(_ps([100e6, 101e6, 102e6, 100.49e6],
                         [-90, -90, -90, -40]))
    assert out[0] == -40


def test_align_rejects_below_min_overlap_and_empty():
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 102e6])
    with pytest.raises(ValueError, match="share enough"):
        grid.align(_ps([5e9, 5.001e9], [-90, -90]))
    with pytest.raises(ValueError, match="empty"):
        grid.align(_ps([], []))


def test_align_at_exactly_min_overlap_passes():
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 102e6, 103e6])
    out = grid.align(_ps([100e6, 103e6], [-90, -80]))
    assert out[0] == -90 and out[3] == -80
    assert np.isnan(out[1]) and np.isnan(out[2])


# --- baseline / diff / pipeline outputs on known inputs ---------------------

def _base(freqs=None):
    freqs = [100e6, 101e6, 102e6] if freqs is None else freqs
    return build_baseline([_ps(freqs, [-90] * len(freqs))] * 3)


def test_aligned_live_fills_missing_with_baseline_mean_and_identity_fast_path():
    base = _base()
    live = _ps([100e6, 102e6], [-90, -40])
    aligned = aligned_live(live, base)
    np.testing.assert_array_equal(aligned.freqs_hz, base.freqs_hz)
    np.testing.assert_array_equal(aligned.power_dbm, [-90, -90, -40])
    same = _ps([100e6, 101e6, 102e6], [-90, -90, -90])
    assert aligned_live(same, base) is same


def test_zscores_and_candidates_on_known_inputs():
    base = _base()
    live = _ps([100e6, 101e6, 102e6], [-90, -50, -90])
    z = zscores(live, base)
    assert z[1] == pytest.approx(20.0)  # ( -50 - -90 ) / STD_FLOOR 2.0
    (c,) = find_candidates(live, base, k=4.0)
    assert c.center_freq_hz == 101e6
    assert c.bin_hz == 1e6 and c.bandwidth_hz == 1e6 and c.n_bins == 1


def test_find_candidates_aligns_once_for_an_off_grid_live_sweep(monkeypatch):
    base = _base()
    calls = 0
    orig = SpectrumGrid.align
    def counting(self, ps):
        nonlocal calls
        calls += 1
        return orig(self, ps)
    monkeypatch.setattr(SpectrumGrid, "align", counting)
    live = _ps([100e6, 102e6, 104e6], [-90, -40, -90])
    (c,) = find_candidates(live, base, k=4.0)
    assert c.center_freq_hz == 102e6
    assert calls == 1


def test_pipeline_parity_on_known_grid():
    plan = SweepPlan.search(300_000_000, 300_200_000, 10_000)
    freqs = np.arange(plan.start_hz, plan.stop_hz, plan.bin_hz, dtype=float)
    quiet = np.full(len(freqs), -95.0)
    base = build_baseline([PowerSpectrum(freqs, quiet, float(t)) for t in range(3)])
    power = quiet.copy()
    power[3] = -30.0
    findings = sweep_findings(PowerSpectrum(freqs, power, 3.0), base, plan)
    assert findings and findings[0].candidate.center_freq_hz == freqs[3]
    assert findings[0].candidate.bin_hz == 10_000


def test_pipeline_rejects_non_uniform_and_mismatched_grids_via_grid_home():
    plan = SweepPlan.search(300_000_000, 300_200_000, 10_000)
    freqs = np.array([300e6, 300.01e6, 300.03e6])
    power = np.full(3, -95.0)
    live = PowerSpectrum(freqs, power, 1.0)
    base = Baseline(freqs, power, np.full(3, 2.0), 1, 0.0)
    with pytest.raises(ValueError, match="not uniform"):
        sweep_findings(live, base, plan)


# --- one-home property -------------------------------------------------------

def test_spacing_and_alignment_are_not_rederived_in_consumers():
    for mod in (pipeline_mod, baseline_mod, diff_mod):
        src = inspect.getsource(mod)
        assert "_spacing_hz" not in src, mod.__name__
        assert "freqs_hz[1] -" not in src, mod.__name__
        assert "np.min(np.diff" not in src, mod.__name__
        assert "np.searchsorted" not in src, mod.__name__


def test_align_preserves_dropped_bin_in_every_sweep_grid():
    # Settled #14 semantics: longest sweep may itself carry the gap.
    grid = SpectrumGrid.from_freqs([100e6, 101e6, 103e6, 104e6])
    out = grid.align(_ps([100e6, 101e6, 103e6, 104e6], [-90, -91, -92, -93]))
    np.testing.assert_array_equal(out, [-90, -91, -92, -93])
    base = build_baseline([
        _ps([100e6, 101e6, 103e6, 104e6], [-90, -90, -90, -90]),
        _ps([100e6, 103e6], [-92, -92]),
    ])
    np.testing.assert_array_equal(base.freqs_hz, [100e6, 101e6, 103e6, 104e6])
    assert base.mean_dbm[0] == pytest.approx(-91.0)
