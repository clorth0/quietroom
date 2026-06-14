import numpy as np

from quietroom.radio.parse import iq_cs8_to_complex


def test_converts_interleaved_cs8_to_complex():
    raw = bytes([64, 0, 0, 64])  # I=64,Q=0 then I=0,Q=64 (int8)
    iq = iq_cs8_to_complex(raw)
    assert iq.dtype == np.complex64
    assert len(iq) == 2
    assert iq[0] == np.complex64(0.5 + 0j)
    assert iq[1] == np.complex64(0 + 0.5j)


def test_odd_trailing_byte_is_dropped():
    raw = bytes([64, 0, 7])  # one stray byte
    iq = iq_cs8_to_complex(raw)
    assert len(iq) == 1


def test_empty_returns_empty():
    iq = iq_cs8_to_complex(b"")
    assert len(iq) == 0
