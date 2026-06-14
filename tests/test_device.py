import numpy as np
import pytest

from quietroom.radio.device import Device, DeviceInfo, DeviceError
from quietroom.engine.spectrum import PowerSpectrum


def test_device_is_abstract():
    with pytest.raises(TypeError):
        Device()  # abstract, cannot instantiate


def test_device_info_fields():
    info = DeviceInfo(serial="abc123", board="HackRF One", firmware="2024.02.1")
    assert info.serial == "abc123"
    assert info.board == "HackRF One"


def test_concrete_subclass_can_implement():
    class Dummy(Device):
        def probe(self):
            return DeviceInfo("s", "b", "f")

        def sweep(self, f_start_hz, f_stop_hz, bin_hz, cycles=1):
            yield PowerSpectrum(np.array([1.0]), np.array([-90.0]), 0.0)

        def capture_iq(self, center_hz, sample_rate, n_samples):
            return np.zeros(n_samples, dtype=np.complex64)

    d = Dummy()
    assert d.probe().serial == "s"
    assert list(d.sweep(0, 1, 1))[0].power_dbm[0] == -90.0
    assert len(d.capture_iq(100, 2_000_000, 8)) == 8
    assert issubclass(DeviceError, Exception)
