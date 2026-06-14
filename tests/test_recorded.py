import numpy as np

from quietroom.radio.recorded import RecordedDevice, demo_device
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.engine.baseline import build_baseline
from quietroom.engine.pipeline import sweep_findings


def _flat(power, t=0.0):
    freqs = np.arange(300e6, 300e6 + 1e6 * 20, 1e6)
    return PowerSpectrum(freqs, np.full(20, power, dtype=float), t)


def test_recorded_device_cycles_sweeps():
    a, b = _flat(-95, 1.0), _flat(-90, 2.0)
    dev = RecordedDevice(sweeps=[a, b], iq=np.zeros(4, dtype=np.complex64))
    out = list(dev.sweep(0, 1, 1, cycles=3))  # cycles beyond list -> wrap around
    assert len(out) == 3
    assert out[0].power_dbm[0] == -95.0
    assert out[2].power_dbm[0] == -95.0  # wrapped back to first
    assert dev.probe().board == "RecordedDevice"
    assert len(dev.capture_iq(100, 2_000_000, 4)) == 4


def test_demo_device_has_a_plantable_bug():
    dev = demo_device()
    baseline = build_baseline(list(dev.sweep(0, 1, 1, cycles=3)))
    live = next(iter(dev.live_sweep()))
    findings = sweep_findings(live, baseline)
    assert findings
    assert findings[0].score > 50.0
