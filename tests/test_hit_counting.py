"""Characterization + one-implementation tests for hit counting (candidate #3).

The counting rules live in exactly one place inside persistence:
``count_hits`` (given a finding and the sightings per sweep, return the
count/confirmation facts). Both call shapes consume it:

- streaming (``PersistenceTracker.update``, web): keeps every finding and
  annotates unconfirmed ones -- ``tests/test_persistence.py`` /
  ``tests/test_session.py`` policy;
- batch (``confirm_findings``, CLI via ``scan.confirmed_findings``):
  drops findings below ``min_hits`` -- ``tests/test_persistence.py`` policy.

Those keep/drop policies differ intentionally and stay at the call sites;
only the counting (one hit per sweep, frequency tolerance, threshold)
is shared.
"""
from quietroom.engine import persistence
from quietroom.engine.persistence import HitCount, PersistenceTracker, confirm_findings, count_hits
from quietroom.engine.score import unsquash
from quietroom.engine.spectrum import Candidate, Finding


def _f(freq, score=50.0, bin_hz=10e3):
    c = Candidate(center_freq_hz=freq, bandwidth_hz=bin_hz, peak_power_dbm=-60.0,
                  snr_over_baseline_db=20.0, bin_hz=bin_hz, n_bins=1)
    return Finding(candidate=c, score=score, band_label="unknown",
                   raw_score=unsquash(score), reasons=["x"], breakdown={"snr": 20.0})


# --- count_hits interface: the counting rules, pinned directly ---

def test_count_hits_counts_sweeps_not_sightings_within_a_sweep():
    count = count_hits(_f(300e6), [[300e6, 300e6], [310e6], [300e6]], min_hits=2)
    assert count == HitCount(hits=2, sweeps=3, min_hits=2)
    assert count.confirmed


def test_count_hits_exactly_at_threshold_is_confirmed_one_below_is_not():
    assert count_hits(_f(300e6), [[300e6], [300e6], []], min_hits=2).confirmed
    below = count_hits(_f(300e6), [[300e6], [], []], min_hits=2)
    assert (below.hits, below.sweeps, below.confirmed) == (1, 3, False)


def test_count_hits_tolerance_is_inclusive_and_defaults_to_one_and_a_half_bins():
    # bin_hz=10 kHz -> default tolerance 15 kHz; exactly at it matches, beyond does not.
    assert count_hits(_f(300e6), [[300.015e6]], min_hits=1).hits == 1
    assert count_hits(_f(300e6), [[300.016e6]], min_hits=1).hits == 0
    assert count_hits(_f(300e6), [[300.02e6]], min_hits=1, tolerance_hz=25e3).hits == 1


def test_count_hits_empty_history_is_zero_of_zero_unconfirmed():
    assert count_hits(_f(300e6), [], min_hits=1) == HitCount(hits=0, sweeps=0, min_hits=1)


# --- Characterization at both call sites, incl. boundaries ---

def test_tracker_duplicate_sightings_in_one_sweep_count_once():
    t = PersistenceTracker(window=5, min_hits=2)
    out = t.update([_f(300e6), _f(300e6)])
    assert all(any("unconfirmed: seen in 1/1 sweeps" in r for r in f.reasons) for f in out)
    (out,) = t.update([_f(300e6)])
    assert any("persistent: seen in 2/2 sweeps" in r for r in out.reasons)


def test_tracker_exactly_at_threshold_confirms_one_below_keeps_unconfirmed():
    t = PersistenceTracker(window=5, min_hits=2)
    (first,) = t.update([_f(300e6)])  # 1 hit, below threshold: kept, unconfirmed (web policy)
    assert any("unconfirmed: seen in 1/1 sweeps" in r for r in first.reasons)
    assert first.breakdown["persistence"] == 0.0
    (second,) = t.update([_f(300e6)])  # 2 hits, exactly at threshold
    assert any("persistent: seen in 2/2 sweeps" in r for r in second.reasons)
    assert second.breakdown["persistence"] > 0


def test_confirm_duplicate_sightings_in_one_sweep_count_once():
    batches = [[_f(300e6), _f(300e6)], [_f(310e6)]]
    assert confirm_findings(batches, min_hits=2) == []  # only 1 sweep hit: dropped (CLI policy)
    out = confirm_findings([[_f(300e6), _f(300e6)], [_f(300e6)]], min_hits=2)
    assert len(out) == 1
    assert any("seen in 2/2 sweeps" in r for r in out[0].reasons)


def test_confirm_exactly_at_threshold_is_kept_one_below_is_dropped():
    batches = [[_f(300e6), _f(310e6)], [_f(300e6)], [_f(310e6)]]
    out = confirm_findings(batches, min_hits=2)  # both seen in exactly 2/3 sweeps
    assert {f.candidate.center_freq_hz for f in out} == {300e6, 310e6}
    assert confirm_findings(batches, min_hits=3) == []


# --- One implementation: both call shapes cross count_hits (failing-first) ---

def test_both_call_shapes_cross_the_one_counting_implementation(monkeypatch):
    def fake_count(finding, sweeps, min_hits, tolerance_hz=None):
        return HitCount(hits=min_hits, sweeps=len(list(sweeps)), min_hits=min_hits)

    monkeypatch.setattr(persistence, "count_hits", fake_count)
    # Streaming policy would normally mark a first sighting unconfirmed.
    t = PersistenceTracker(window=5, min_hits=2)
    (tracked,) = t.update([_f(300e6)])
    assert any("persistent" in r for r in tracked.reasons)
    # Batch policy would normally drop a single-sweep transient.
    kept = confirm_findings([[_f(300e6)], [_f(310e6)]], min_hits=2)
    assert {f.candidate.center_freq_hz for f in kept} == {300e6, 310e6}
