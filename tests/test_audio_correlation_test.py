import numpy as np

from quietroom.audio.capture import audio_correlation_test
from quietroom.audio.signal import make_chirp
from quietroom.radio.device import Device, DeviceInfo


class _FakeIQDevice(Device):
    """capture_iq returns an AM signal whose envelope is the given audio at iq_rate."""

    def __init__(self, audio_at_iq_rate):
        self._mod = audio_at_iq_rate

    def probe(self):
        return DeviceInfo("fake", "Fake", "n/a")

    def sweep(self, f_start_hz, f_stop_hz, bin_hz, cycles=1):
        yield from ()

    def capture_iq(self, center_hz, sample_rate, n_samples):
        mod = self._mod[:n_samples]
        t = np.arange(len(mod)) / sample_rate
        carrier = np.exp(1j * 2 * np.pi * 1000 * t)
        return ((1.0 + 0.5 * mod) * carrier).astype(np.complex64)


def test_audio_correlation_high_when_rf_carries_the_room_audio():
    iq_rate = 200_000
    duration = 0.5
    audio_fs = 8_000
    chirp = make_chirp(duration, audio_fs)

    # The "room audio" recorded by the mic is the chirp; the bug's RF envelope
    # carries that same chirp (resampled up to the IQ rate).
    n_iq = int(iq_rate * duration)
    from quietroom.audio.signal import resample_to
    mod_at_iq = resample_to(chirp, n_iq)

    dev = _FakeIQDevice(mod_at_iq)

    def fake_play_record(signal, fs):
        return chirp.astype(float)          # mic "hears" the chirp

    corr = audio_correlation_test(
        dev, 308_000_000,
        sample_rate=iq_rate, duration_s=duration, audio_fs=audio_fs,
        play_record=fake_play_record,
    )
    assert corr > 0.8


def test_audio_correlation_low_when_rf_unrelated():
    iq_rate = 200_000
    duration = 0.5
    audio_fs = 8_000
    rng = np.random.default_rng(0)
    dev = _FakeIQDevice(rng.normal(size=int(iq_rate * duration)))

    def fake_play_record(signal, fs):
        return make_chirp(duration, audio_fs).astype(float)

    corr = audio_correlation_test(
        dev, 308_000_000,
        sample_rate=iq_rate, duration_s=duration, audio_fs=audio_fs,
        play_record=fake_play_record,
    )
    assert corr < 0.5
