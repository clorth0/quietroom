import numpy as np

from quietroom.audio.signal import iq_to_fm_envelope


def test_fm_envelope_recovers_frequency_modulation():
    in_fs = 200_000
    out_fs = 8_000
    t = np.arange(in_fs) / in_fs                 # 1 second
    msg = np.sin(2 * np.pi * 5 * t)              # 5 Hz message
    # FM: instantaneous phase is the integral of the message.
    dev = 10_000.0
    phase = 2 * np.pi * (1000 * t + dev * np.cumsum(msg) / in_fs)
    iq = np.exp(1j * phase).astype(np.complex64)

    env = iq_to_fm_envelope(iq, in_fs, out_fs)
    assert len(env) == out_fs

    ref = np.sin(2 * np.pi * 5 * (np.arange(out_fs) / out_fs))
    corr = np.corrcoef(env, ref)[0, 1]
    assert abs(corr) > 0.9


def test_fm_envelope_too_short():
    assert len(iq_to_fm_envelope(np.zeros(1, dtype=np.complex64), 200_000, 8_000)) == 0
