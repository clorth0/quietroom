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


def test_single_bin_at_coarse_resolution_gets_partial_credit():
    # A 1-bin hit at 1 MHz resolution cannot prove narrowband, but it is
    # still unresolved and should not be ignored entirely.
    c = Candidate(center_freq_hz=200e6, bandwidth_hz=1e6, peak_power_dbm=-70.0,
                  snr_over_baseline_db=20.0, bin_hz=1e6, n_bins=1)
    r = score_signatures(c)
    assert "narrowband carrier" not in r.reasons
    assert any("unresolved single-bin" in s for s in r.reasons)
    assert 0 < r.contribution < 15.0


def test_multi_bin_coarse_signal_gets_no_narrowband_credit():
    c = Candidate(center_freq_hz=200e6, bandwidth_hz=3e6, peak_power_dbm=-70.0,
                  snr_over_baseline_db=20.0, bin_hz=1e6, n_bins=3)
    assert score_signatures(c).contribution == 0.0
