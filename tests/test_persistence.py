from quietroom.engine.persistence import PersistenceTracker, confirm_findings
from quietroom.engine.score import unsquash
from quietroom.engine.spectrum import Candidate, Finding


def _f(freq, score=50.0, bin_hz=10e3):
    c = Candidate(center_freq_hz=freq, bandwidth_hz=bin_hz, peak_power_dbm=-60.0,
                  snr_over_baseline_db=20.0, bin_hz=bin_hz, n_bins=1)
    return Finding(candidate=c, score=score, band_label="unknown",
                   raw_score=unsquash(score), reasons=["x"], breakdown={"snr": 20.0})


def test_first_sighting_is_marked_unconfirmed_without_bonus():
    t = PersistenceTracker(window=5, min_hits=2)
    (out,) = t.update([_f(300e6)])
    assert out.score == 50.0
    assert any("unconfirmed" in r for r in out.reasons)


def test_repeat_sighting_earns_persistence_bonus():
    t = PersistenceTracker(window=5, min_hits=2)
    t.update([_f(300e6)])
    (out,) = t.update([_f(300e6)])
    assert out.score > 50.0
    assert any("persistent" in r and "2/2" in r for r in out.reasons)
    assert out.breakdown["persistence"] > 0


def test_small_frequency_drift_still_matches():
    t = PersistenceTracker(window=5, min_hits=2, tolerance_hz=15e3)
    t.update([_f(300e6)])
    (out,) = t.update([_f(300.01e6)])
    assert any("persistent" in r for r in out.reasons)


def test_window_forgets_old_sightings():
    t = PersistenceTracker(window=2, min_hits=2)
    t.update([_f(300e6)])
    t.update([])
    t.update([])
    (out,) = t.update([_f(300e6)])
    assert any("unconfirmed" in r for r in out.reasons)


def test_confirm_findings_drops_transients_over_a_batch():
    batches = [[_f(300e6), _f(310e6)], [_f(300e6)], [_f(300e6)]]
    out = confirm_findings(batches, min_hits=2)
    assert [f.candidate.center_freq_hz for f in out] == [300e6]
    assert any("3/3" in r for r in out[0].reasons)


def test_confirm_findings_sorted_by_score():
    batches = [[_f(300e6, 40), _f(310e6, 60)], [_f(300e6, 40), _f(310e6, 60)]]
    out = confirm_findings(batches, min_hits=2)
    assert [f.score for f in out] == sorted([f.score for f in out], reverse=True)
