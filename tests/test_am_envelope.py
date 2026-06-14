import numpy as np

from quietroom.audio.signal import iq_to_am_envelope


def test_am_envelope_recovers_amplitude_modulation():
    in_fs = 200_000
    out_fs = 8_000
    t = np.arange(in_fs) / in_fs                 # 1 second
    mod = 1.0 + 0.5 * np.sin(2 * np.pi * 5 * t)  # 5 Hz amplitude modulation
    carrier_offset = np.exp(1j * 2 * np.pi * 1000 * t)
    iq = (mod * carrier_offset).astype(np.complex64)

    env = iq_to_am_envelope(iq, in_fs, out_fs)
    assert len(env) == out_fs                    # 1 second at out_fs

    # The recovered envelope should track the 5 Hz modulation.
    ref = np.sin(2 * np.pi * 5 * (np.arange(out_fs) / out_fs))
    corr = np.corrcoef(env, ref)[0, 1]
    assert abs(corr) > 0.9


def test_am_envelope_empty_input():
    assert len(iq_to_am_envelope(np.zeros(0, dtype=np.complex64), 200_000, 8_000)) == 0
