import numpy as np

from quietroom.audio.capture import CaptureFailed, LiveBug, NoAudioMatch, audio_correlation_test
from quietroom.cli import format_verdict
from quietroom.engine.spectrum import Candidate
from quietroom.radio.device import Device, DeviceError, DeviceInfo
from quietroom.radio.recorded import demo_device
from quietroom.scan import investigate


def _candidate():
    # 308 MHz, narrowband, strong: catalog(unknown)+signatures already make it suspicious.
    return Candidate(center_freq_hz=308_000_000.0, bandwidth_hz=1_000_000.0,
                     peak_power_dbm=-30.0, snr_over_baseline_db=65.0)


def _loopback(sig, fs):
    return np.asarray(sig, float)


class _FixedIQ(Device):
    def __init__(self, samples):
        self._samples = samples

    def probe(self):
        return DeviceInfo("fixed", "Fixed", "n/a")

    def sweep(self, *a, **k):
        yield from ()

    def capture_iq(self, center_hz, sample_rate, n_samples):
        return self._samples


def test_investigate_high_correlation_raises_score_and_adds_reason():
    finding = investigate(
        demo_device(), _candidate(), play_record=_loopback, duration_s=0.25,
    )
    assert any("correlates with room audio" in r for r in finding.reasons)
    assert finding.score >= 90.0
    assert "audio" in finding.breakdown


def test_investigate_low_correlation_no_audio_reason():
    rng = np.random.default_rng(1)

    def capture_noise(center_hz, sample_rate, n_samples):
        noise = rng.normal(size=n_samples) + 1j * rng.normal(size=n_samples)
        return noise.astype(np.complex64)

    class _Noise(Device):
        def probe(self):
            return DeviceInfo("n", "n", "n")

        def sweep(self, *a, **k):
            yield from ()

        def capture_iq(self, center_hz, sample_rate, n_samples):
            return capture_noise(center_hz, sample_rate, n_samples)

    finding = investigate(_Noise(), _candidate(), play_record=_loopback, duration_s=0.05)
    assert not any("correlates with room audio" in r for r in finding.reasons)
    assert finding.breakdown["audio"] == 0.0


def test_empty_and_short_captures_are_not_a_quiet_room():
    empty = audio_correlation_test(
        _FixedIQ(np.zeros(0, dtype=np.complex64)), 308_000_000,
        sample_rate=20_000, duration_s=0.05, play_record=_loopback,
    )
    short = audio_correlation_test(
        _FixedIQ(np.zeros(8, dtype=np.complex64)), 308_000_000,
        sample_rate=20_000, duration_s=0.05, play_record=_loopback,
    )
    assert isinstance(empty, CaptureFailed)
    assert isinstance(short, CaptureFailed)
    for verdict in (empty, short):
        text = format_verdict(308_000_000.0, verdict)
        assert "capture failed" in text
        assert "not a detectable live audio bug" not in text


def test_real_chirp_is_a_live_bug_and_noise_is_not():
    from quietroom.audio.signal import make_chirp, resample_to

    iq_rate = 200_000
    duration = 0.5
    audio_fs = 8_000
    chirp = make_chirp(duration, audio_fs)
    n_iq = int(iq_rate * duration)
    mod = resample_to(chirp, n_iq)
    t = np.arange(n_iq) / iq_rate
    # audio_correlation_test tunes a quarter sample-rate above the target.
    center = 308_000_000 + int(iq_rate * 0.25)
    carrier = np.exp(1j * 2 * np.pi * (308_000_000 - center) * t)
    iq = ((1.0 + 0.5 * mod) * carrier).astype(np.complex64)
    live = audio_correlation_test(
        _FixedIQ(iq), 308_000_000,
        sample_rate=iq_rate, duration_s=duration, audio_fs=audio_fs,
        play_record=lambda signal, fs: chirp.astype(float),
    )
    assert isinstance(live, LiveBug)
    assert live.correlation > 0.8

    rng = np.random.default_rng(2)
    noise = (rng.normal(size=n_iq) + 1j * rng.normal(size=n_iq)).astype(np.complex64)
    quiet = audio_correlation_test(
        _FixedIQ(noise), 308_000_000,
        sample_rate=iq_rate, duration_s=duration, audio_fs=audio_fs,
        play_record=lambda signal, fs: chirp.astype(float),
    )
    assert isinstance(quiet, NoAudioMatch)
    assert quiet.correlation < 0.5


def test_device_error_becomes_capture_failed_and_investigate_raises():
    class _Dead(Device):
        def probe(self):
            return DeviceInfo("d", "d", "d")

        def sweep(self, *a, **k):
            yield from ()

        def capture_iq(self, *a, **k):
            raise DeviceError("hackrf_transfer: Resource busy")

    verdict = audio_correlation_test(
        _Dead(), 308_000_000, duration_s=0.05, play_record=_loopback,
    )
    assert isinstance(verdict, CaptureFailed)
    assert "Resource busy" in verdict.reason
    try:
        investigate(_Dead(), _candidate(), play_record=_loopback, duration_s=0.05)
    except DeviceError as exc:
        assert "Resource busy" in str(exc)
    else:
        raise AssertionError("expected DeviceError")
