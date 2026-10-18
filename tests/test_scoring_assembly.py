"""Characterization + assembled-once tests for the deep scoring assembly.

Both operator paths (fresh sweep and investigate) must cross the same
scoring seam: one detector roster, one band labelling, one fusion.
"""
import numpy as np
import pytest

from quietroom.engine import score as scoring
from quietroom.engine.baseline import build_baseline
from quietroom.engine.persistence import PersistenceTracker, confirm_findings
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.engine.spectrum import Candidate, DetectorResult, PowerSpectrum
from quietroom.radio.recorded import demo_device
from quietroom.scan import investigate

_PLAN = SweepPlan.search(300_000_000, 300_200_000, 10_000)


def _flat(power_dbm, t=0.0):
    freqs = np.arange(300e6, 300e6 + 10e3 * 20, 10e3)
    return PowerSpectrum(freqs_hz=freqs, power_dbm=np.full(20, power_dbm, dtype=float),
                         timestamp=t)


def _planted():
    base = build_baseline([_flat(-95), _flat(-95), _flat(-95)])
    live = _flat(-95)
    live.power_dbm[8] = -30.0  # 300.08 MHz, unknown + covert band, narrow, near-field
    return sweep_findings(live, base, _PLAN)


def _candidate():
    return Candidate(center_freq_hz=308_000_000.0, bandwidth_hz=1_000_000.0,
                     peak_power_dbm=-30.0, snr_over_baseline_db=65.0)


def _loopback(sig, fs):
    return np.asarray(sig, float)


# --- Characterization: pin current outputs on both paths (pre-refactor) ---

def test_sweep_path_output_is_pinned():
    (f,) = _planted()
    assert f.candidate.center_freq_hz == 300_080_000.0
    assert f.score == pytest.approx(83.76793888181518)
    assert f.raw_score == 100.0
    assert f.band_label == "unknown"
    assert list(f.breakdown) == ["snr", "catalog", "signatures"]
    assert f.breakdown == {"snr": 20.0, "catalog": 30.0, "signatures": 50.0}
    assert f.reasons == [
        "not in any known band", "narrowband carrier",
        "near-field strong signal", "in common analog bug band",
    ]


def test_investigate_path_output_is_pinned():
    f = investigate(demo_device(), _candidate(), play_record=_loopback, duration_s=0.25)
    assert f.score == pytest.approx(91.40977669621238)
    assert f.raw_score == 135.0
    assert f.band_label == "unknown"
    assert list(f.breakdown) == ["snr", "catalog", "signatures", "audio"]
    assert f.breakdown == {"snr": 20.0, "catalog": 30.0, "signatures": 35.0, "audio": 50.0}
    assert f.reasons[-1].startswith("envelope correlates with room audio at ")


def test_both_paths_agree_on_core_detectors_and_label():
    (swept,) = _planted()
    # Same candidate shape at 300.08 MHz through investigate's candidate form.
    cand = Candidate(center_freq_hz=300_080_000.0, bandwidth_hz=10_000.0,
                     peak_power_dbm=-30.0, snr_over_baseline_db=65.0)
    inv = investigate(demo_device(), cand, play_record=_loopback, duration_s=0.25)
    assert inv.band_label == swept.band_label
    for name in ("catalog", "signatures"):
        assert inv.breakdown[name] == swept.breakdown[name]
    assert inv.raw_score - swept.raw_score == inv.breakdown["audio"]


def test_persistence_annotation_output_is_pinned():
    tracker = PersistenceTracker(window=5, min_hits=2)
    (first,) = tracker.update(_planted())
    assert first.score == pytest.approx(83.76793888181518)
    assert first.raw_score == 100.0
    assert first.breakdown["persistence"] == 0.0
    assert first.reasons[-1] == "unconfirmed: seen in 1/1 sweeps"
    (second,) = tracker.update(_planted())
    assert second.score == pytest.approx(86.46647167633873)
    assert second.raw_score == 110.0
    assert list(second.breakdown) == ["snr", "catalog", "signatures", "persistence"]
    assert second.breakdown["persistence"] == 10.0
    assert second.reasons[-1] == "persistent: seen in 2/2 sweeps"
    confirmed = confirm_findings([_planted(), _planted()], min_hits=2)
    assert len(confirmed) == 1
    assert confirmed[0].breakdown["persistence"] == 10.0
    assert confirmed[0].reasons[-1] == "persistent: seen in 2/2 sweeps"


# --- Assembled once: one registration runs on both paths (failing-first) ---

def _test_detector(candidate, band):
    return DetectorResult("testonly", 7.0, ["test-only detector fired"])


def test_one_roster_registration_runs_on_both_paths(monkeypatch):
    monkeypatch.setattr(
        scoring, "DETECTOR_ROSTER", scoring.DETECTOR_ROSTER + (_test_detector,),
    )
    (swept,) = _planted()
    assert swept.breakdown["testonly"] == 7.0
    assert "test-only detector fired" in swept.reasons
    inv = investigate(demo_device(), _candidate(), play_record=_loopback, duration_s=0.25)
    assert inv.breakdown["testonly"] == 7.0
    assert "test-only detector fired" in inv.reasons


def test_assembly_interface_scores_and_labels_in_one_place():
    f = scoring.assemble_finding(_candidate())
    assert f.band_label == "unknown"
    assert list(f.breakdown) == ["snr", "catalog", "signatures"]
    assert scoring.band_label_for(98_500_000.0) == "FM broadcast"
    assert scoring.band_label_for(380_000_000.0) == "unknown"
