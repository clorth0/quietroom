import numpy as np
import pytest

from quietroom.radio.capture import IqCapture, require_iq
from quietroom.radio.device import DeviceError


def test_require_iq_accepts_a_full_buffer():
    samples = np.zeros(1000, dtype=np.complex64)
    assert len(require_iq(samples, 1000)) == 1000
    IqCapture(samples, 1000)


def test_require_iq_rejects_empty_and_short_buffers():
    empty = np.zeros(0, dtype=np.complex64)
    short = np.zeros(8, dtype=np.complex64)
    with pytest.raises(DeviceError, match="0 samples"):
        require_iq(empty, 1000)
    with pytest.raises(DeviceError, match="8 samples"):
        require_iq(short, 1000)
    with pytest.raises(DeviceError):
        IqCapture(empty, 1000)
    with pytest.raises(DeviceError):
        IqCapture(short, 1000)
