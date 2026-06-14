import numpy as np
import pytest

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.engine.baseline import build_baseline, zscores


def _spectrum(power, t=0.0):
    return PowerSpectrum(
        freqs_hz=np.array([100e6, 101e6, 102e6]),
        power_dbm=np.array(power, dtype=float),
        timestamp=t,
    )


def test_build_baseline_computes_mean_and_std():
    base = build_baseline([
        _spectrum([-90, -80, -85], t=1.0),
        _spectrum([-92, -78, -85], t=2.0),
    ])
    assert base.sweep_count == 2
    np.testing.assert_allclose(base.mean_dbm, [-91, -79, -85])
    assert base.created_at == 2.0
    assert base.std_dbm[2] == 0.0


def test_build_baseline_rejects_mismatched_bins():
    a = _spectrum([-90, -90, -90])
    b = PowerSpectrum(
        freqs_hz=np.array([1.0, 2.0]),
        power_dbm=np.array([-90.0, -90.0]),
        timestamp=0.0,
    )
    with pytest.raises(ValueError):
        build_baseline([a, b])


def test_build_baseline_rejects_empty():
    with pytest.raises(ValueError):
        build_baseline([])


def test_zscores_flag_excess_power():
    base = build_baseline([
        _spectrum([-90, -90, -90]),
        _spectrum([-90, -90, -90]),
    ])
    live = _spectrum([-90, -50, -90])  # big jump in bin 1
    z = zscores(live, base)
    # std is floored, so bin 1 should show a large positive z and others ~0.
    assert z[1] > z[0]
    assert z[1] > z[2]
    assert z[0] == pytest.approx(0.0, abs=1e-9)
