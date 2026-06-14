from quietroom.engine.spectrum import Candidate
from quietroom.engine.detectors.catalog import score_band


def _cand(freq):
    return Candidate(center_freq_hz=freq, bandwidth_hz=20e3,
                     peak_power_dbm=-50.0, snr_over_baseline_db=20.0)


def test_known_band_lowers_suspicion():
    r = score_band(_cand(98_500_000.0))  # FM broadcast
    assert r.name == "catalog"
    assert r.contribution < 0
    assert "FM" in r.reasons[0]


def test_unknown_band_raises_suspicion():
    r = score_band(_cand(380_000_000.0))  # deliberate catalog gap
    assert r.contribution > 0
    assert "not in any known band" in r.reasons
