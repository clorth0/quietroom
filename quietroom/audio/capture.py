"""Hardware wrapper for the audio-correlation test (sounddevice + HackRF IQ)."""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from quietroom.audio.signal import (
    best_lag_correlation,
    channelize,
    iq_to_am_envelope,
    iq_to_fm_envelope,
    make_chirp,
    resample_to,
)
from quietroom.engine.detectors.audio_corr import AUDIO_CORR_THRESHOLD
from quietroom.radio.capture import IqCapture
from quietroom.radio.device import Device, DeviceError

DEFAULT_AUDIO_FS = 48_000
DEFAULT_DURATION_S = 1.0
DEFAULT_IQ_RATE = 2_000_000
ENVELOPE_FS = 8_000          # common rate at which audio + RF envelopes are compared
MAX_LAG_S = 0.25
# Tune this fraction of the sample rate above the target so the target sits
# clear of the HackRF DC spike; the channel filter then removes the spike.
IQ_OFFSET_FRACTION = 0.25
# Channel width kept around the target (covers NFM/WFM-ish bugs up to ~100 kHz).
CHANNEL_BW_HZ = 100_000             # search +/- 0.25 s of latency between playback and capture


def play_and_record(signal: np.ndarray, fs: int) -> np.ndarray:
    """Play `signal` through the speaker while recording the mic; return mono recording.

    sounddevice is imported lazily here because importing it requires the PortAudio
    library at runtime, which is absent on headless CI. No other code path imports it.
    """
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:   # OSError: PortAudio library missing
        raise RuntimeError(
            "the audio-correlation test needs sounddevice and PortAudio: "
            "install with `uv sync --extra audio` (or pip install 'quietroom[audio]')"
        ) from e

    rec = sd.playrec(np.asarray(signal).reshape(-1, 1), samplerate=fs, channels=1)
    sd.wait()
    return np.asarray(rec).reshape(-1).astype(float)


@dataclass(frozen=True)
class LiveBug:
    """The RF envelope moves with the room audio."""

    correlation: float


@dataclass(frozen=True)
class NoAudioMatch:
    """The capture completed and the envelope does not match the room."""

    correlation: float


@dataclass(frozen=True)
class CaptureFailed:
    """The radio did not return the IQ that was asked for."""

    reason: str


AudioVerdict = LiveBug | NoAudioMatch | CaptureFailed


def audio_correlation_test(
    device: Device,
    center_hz: int,
    *,
    sample_rate: int = DEFAULT_IQ_RATE,
    duration_s: float = DEFAULT_DURATION_S,
    audio_fs: int = DEFAULT_AUDIO_FS,
    play_record=play_and_record,
) -> AudioVerdict:
    """Play a chirp, capture IQ at center_hz concurrently, and return a verdict.

    Correlates the RF envelope (best of AM/FM) against the recorded room audio,
    tolerant to playback/capture latency. A failed or short capture is
    CaptureFailed, never a low correlation.
    """
    chirp = make_chirp(duration_s, audio_fs)
    n_iq = int(sample_rate * duration_s)

    offset_hz = int(sample_rate * IQ_OFFSET_FRACTION)
    channel_bw = min(CHANNEL_BW_HZ, offset_hz)   # keep the DC spike out of band
    captured: dict[str, np.ndarray] = {}
    failure: list[Exception] = []

    def _capture() -> None:
        try:
            captured["iq"] = device.capture_iq(int(center_hz) + offset_hz, sample_rate, n_iq)
        except Exception as e:  # noqa: BLE001 - reported as CaptureFailed
            failure.append(e)

    thread = threading.Thread(target=_capture)
    thread.start()
    try:
        mic = play_record(chirp, audio_fs)      # blocks ~duration_s, runs with capture
    finally:
        thread.join()
    if failure:
        return CaptureFailed(str(failure[0]))

    iq = captured.get("iq", np.zeros(0, dtype=np.complex64))
    try:
        iq = IqCapture(iq, n_iq).samples
    except DeviceError as e:
        return CaptureFailed(str(e))

    # Bring the recorded room audio and both RF envelopes to a common rate.
    ref = resample_to(mic, max(int(len(mic) * ENVELOPE_FS / audio_fs), 1))
    ch, ch_fs = channelize(iq, sample_rate, shift_hz=offset_hz, bandwidth_hz=channel_bw)
    am = iq_to_am_envelope(ch, ch_fs, ENVELOPE_FS)
    fm = iq_to_fm_envelope(ch, ch_fs, ENVELOPE_FS)
    max_lag = int(MAX_LAG_S * ENVELOPE_FS)
    correlation = max(
        best_lag_correlation(am, ref, max_lag),
        best_lag_correlation(fm, ref, max_lag),
    )
    if correlation >= AUDIO_CORR_THRESHOLD:
        return LiveBug(correlation)
    return NoAudioMatch(correlation)
