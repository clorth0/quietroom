from quietroom.engine.spectrum import Candidate, DetectorResult
from quietroom.engine.score import score_candidate


def _cand(freq=380e6, snr=18.0):
    return Candidate(center_freq_hz=freq, bandwidth_hz=10e3,
                     peak_power_dbm=-35.0, snr_over_baseline_db=snr)


def test_score_sums_contributions_and_saturates():
    results = [
        DetectorResult("catalog", 30.0, ["not in any known band"]),
        DetectorResult("signatures", 35.0, ["narrowband carrier",
                                            "near-field strong signal"]),
        DetectorResult("audio", 50.0, ["envelope correlates with room audio at 0.82"]),
    ]
    f = score_candidate(_cand(), results)
    assert 90.0 < f.score < 100.0   # saturates, never pins at 100
    assert "narrowband carrier" in f.reasons
    assert f.breakdown["audio"] == 50.0
    assert "snr" in f.breakdown


def test_known_band_can_pull_score_down():
    results = [
        DetectorResult("catalog", -40.0, ["in known FM broadcast band"]),
        DetectorResult("signatures", 0.0, []),
        DetectorResult("audio", 0.0, []),
    ]
    f = score_candidate(_cand(freq=98_500_000.0, snr=10.0), results)
    assert f.score < 20.0
    assert f.band_label == "FM broadcast"


def test_band_label_unknown_when_uncatalogued():
    f = score_candidate(_cand(freq=380e6), [])
    assert f.band_label == "unknown"


def test_score_never_negative():
    results = [DetectorResult("catalog", -40.0, [])]
    f = score_candidate(_cand(freq=98_500_000.0, snr=0.0), results)
    assert f.score >= 0.0


def _strong(extra):
    base = [DetectorResult("catalog", 30.0, []), DetectorResult("signatures", 42.0, [])]
    return base + [DetectorResult("x", extra, [])]


def test_strong_suspects_keep_distinct_ordered_scores():
    # Raw evidence 92, 102, 142: all used to clamp to 100 and tie.
    scores = [score_candidate(_cand(snr=20.0), _strong(e)).score for e in (0.0, 10.0, 50.0)]
    assert scores == sorted(scores)
    assert len({round(s) for s in scores}) == 3     # distinct even when displayed
    assert all(s < 100.0 for s in scores)


def test_raw_score_is_exposed():
    f = score_candidate(_cand(snr=20.0), _strong(10.0))
    assert f.raw_score == 20.0 + 30.0 + 42.0 + 10.0


def test_squash_is_monotonic_and_invertible():
    from quietroom.engine.score import squash, unsquash
    xs = [0, 10, 50, 90, 140, 200]
    ys = [squash(x) for x in xs]
    assert ys == sorted(ys) and ys[0] == 0.0 and ys[-1] < 100.0
    for x in xs[:-1]:
        assert abs(unsquash(squash(x)) - x) < 1e-6
