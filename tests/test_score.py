from quietroom.engine.spectrum import Candidate, DetectorResult
from quietroom.engine.score import score_candidate


def _cand(freq=380e6, snr=18.0):
    return Candidate(center_freq_hz=freq, bandwidth_hz=10e3,
                     peak_power_dbm=-35.0, snr_over_baseline_db=snr)


def test_score_sums_contributions_and_clamps():
    results = [
        DetectorResult("catalog", 30.0, ["not in any known band"]),
        DetectorResult("signatures", 35.0, ["narrowband carrier",
                                            "near-field strong signal"]),
        DetectorResult("audio", 50.0, ["envelope correlates with room audio at 0.82"]),
    ]
    f = score_candidate(_cand(), results)
    assert f.score == 100.0  # clamped
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
