import numpy as np

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.engine.baseline import build_baseline
from quietroom.engine.detectors.diff import find_candidates


def _flat(power_dbm, t=0.0):
    freqs = np.arange(100e6, 100e6 + 10e3 * 10, 10e3)  # 10 bins, 10 kHz apart
    return PowerSpectrum(
        freqs_hz=freqs,
        power_dbm=np.full(10, power_dbm, dtype=float),
        timestamp=t,
    )


def test_identical_to_baseline_yields_no_candidates():
    base = build_baseline([_flat(-90), _flat(-90), _flat(-90)])
    live = _flat(-90)
    assert find_candidates(live, base) == []


def test_planted_carrier_becomes_one_candidate():
    base = build_baseline([_flat(-90), _flat(-90), _flat(-90)])
    live = _flat(-90)
    live.power_dbm[4] = -40.0  # one strong bin
    cands = find_candidates(live, base, k=4.0)
    assert len(cands) == 1
    c = cands[0]
    assert c.center_freq_hz == live.freqs_hz[4]
    assert c.peak_power_dbm == -40.0
    assert c.snr_over_baseline_db > 40.0


def test_two_separated_carriers_become_two_candidates():
    base = build_baseline([_flat(-90), _flat(-90), _flat(-90)])
    live = _flat(-90)
    live.power_dbm[2] = -50.0
    live.power_dbm[7] = -55.0
    cands = find_candidates(live, base, k=4.0)
    assert len(cands) == 2
