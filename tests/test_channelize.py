import numpy as np

from quietroom.audio.capture import IQ_OFFSET_FRACTION, audio_correlation_test
from quietroom.audio.signal import (
    best_lag_correlation, channelize, iq_to_am_envelope, make_chirp, resample_to,
)
from quietroom.radio.device import Device, DeviceInfo

FS = 2_000_000
OFF = 500_000


def _scene(n=200_000, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    mod = resample_to(make_chirp(n / FS, 8_000), n)
    target = (1.0 + 0.5 * mod) * np.exp(-1j * 2 * np.pi * OFF * t)
    dc_spike = 5.0 * (1.0 + 0.05 * rng.normal(size=n))   # LO leakage
    interferer = 3.0 * (1 + 1.0 * rng.normal(size=n)) * np.exp(1j * 2 * np.pi * 300_000 * t)
    return (target + dc_spike + interferer).astype(np.complex64), mod


def test_raw_capture_is_swamped_by_dc_and_neighbours():
    iq, mod = _scene()
    env = iq_to_am_envelope(iq, FS, 8_000)
    ref = resample_to(mod, len(env))
    assert best_lag_correlation(env, ref, 100) < 0.5


def test_channelize_recovers_the_target_envelope():
    iq, mod = _scene()
    ch, ch_fs = channelize(iq, FS, shift_hz=OFF, bandwidth_hz=100_000)
    assert ch_fs < FS
    env = iq_to_am_envelope(ch, ch_fs, 8_000)
    ref = resample_to(mod, len(env))
    assert best_lag_correlation(env, ref, 100) > 0.9


class _Recorder(Device):
    def __init__(self):
        self.centers = []

    def probe(self):
        return DeviceInfo("r", "R", "n/a")

    def sweep(self, *a, **k):
        yield from ()

    def capture_iq(self, center_hz, sample_rate, n_samples):
        self.centers.append(center_hz)
        return np.zeros(n_samples, dtype=np.complex64)


def test_correlation_tunes_off_the_target_to_dodge_the_dc_spike():
    dev = _Recorder()
    audio_correlation_test(dev, 308_000_000, sample_rate=FS, duration_s=0.05,
                           audio_fs=8_000, play_record=lambda s, fs: np.asarray(s, float))
    assert dev.centers == [308_000_000 + int(FS * IQ_OFFSET_FRACTION)]


def test_demo_device_runs_the_real_pipeline():
    from quietroom.audio.capture import LiveBug
    from quietroom.radio.recorded import demo_device
    verdict = audio_correlation_test(
        demo_device(), 308_000_000, duration_s=0.25,
        play_record=lambda s, fs: np.asarray(s, float),
    )
    assert isinstance(verdict, LiveBug)
    assert verdict.correlation > 0.8
