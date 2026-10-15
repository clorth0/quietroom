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


def _strong(freq, power=-30.0):
    return Candidate(center_freq_hz=freq, bandwidth_hz=20e3,
                     peak_power_dbm=power, snr_over_baseline_db=40.0)


def test_broadcast_band_keeps_full_discount():
    r = score_band(_cand(98_500_000.0))
    assert r.contribution <= -30.0


def test_shared_band_weak_signal_gets_only_small_discount():
    # 2.4 GHz is where WiFi/BLE bugs live; a distant AP is still explained,
    # but only mildly.
    r = score_band(_cand(2_437_000_000.0))
    assert -15.0 <= r.contribution < 0


def test_shared_band_near_field_emitter_is_not_discounted():
    r = score_band(_strong(2_437_000_000.0))
    assert r.contribution >= 0
    assert any("shared" in s for s in r.reasons)


def test_cellular_near_field_emitter_is_not_discounted():
    r = score_band(_strong(1_900_000_000.0))  # GSM/LTE bugs
    assert r.contribution >= 0


def test_broadcast_near_field_still_discounted():
    r = score_band(_strong(98_500_000.0))
    assert r.contribution < 0
