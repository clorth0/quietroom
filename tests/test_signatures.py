from quietroom.engine.spectrum import Candidate
from quietroom.engine.detectors.signatures import score_signatures


def _cand(freq=200e6, bw=20e3, power=-70.0):
    return Candidate(center_freq_hz=freq, bandwidth_hz=bw,
                     peak_power_dbm=power, snr_over_baseline_db=20.0)


def test_narrowband_flag():
    r = score_signatures(_cand(bw=10e3))
    assert "narrowband carrier" in r.reasons
    assert r.contribution > 0


def test_wideband_is_not_narrowband():
    r = score_signatures(_cand(bw=5_000_000.0, power=-95.0))
    assert "narrowband carrier" not in r.reasons


def test_near_field_strong_flag():
    r = score_signatures(_cand(power=-30.0))
    assert "near-field strong signal" in r.reasons


def test_covert_band_flag():
    r = score_signatures(_cand(freq=380e6))  # inside 300-470 MHz covert band
    assert any("analog bug band" in reason for reason in r.reasons)


def test_clean_candidate_scores_zero():
    r = score_signatures(_cand(freq=200e6, bw=5_000_000.0, power=-95.0))
    assert r.contribution == 0.0
    assert r.reasons == []
