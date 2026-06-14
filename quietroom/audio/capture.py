"""Hardware wrapper for the audio-correlation test (sounddevice + HackRF IQ)."""
from __future__ import annotations

import threading

import numpy as np

from quietroom.audio.signal import (
    best_lag_correlation,
    iq_to_am_envelope,
    iq_to_fm_envelope,
    make_chirp,
    resample_to,
)
from quietroom.radio.device import Device

DEFAULT_AUDIO_FS = 48_000
DEFAULT_DURATION_S = 1.0
DEFAULT_IQ_RATE = 2_000_000
ENVELOPE_FS = 8_000          # common rate at which audio + RF envelopes are compared
MAX_LAG_S = 0.25             # search +/- 0.25 s of latency between playback and capture


def play_and_record(signal: np.ndarray, fs: int) -> np.ndarray:
    """Play `signal` through the speaker while recording the mic; return mono recording.

    sounddevice is imported lazily here because importing it requires the PortAudio
    library at runtime, which is absent on headless CI. No other code path imports it.
    """
    import sounddevice as sd

    rec = sd.playrec(np.asarray(signal).reshape(-1, 1), samplerate=fs, channels=1)
    sd.wait()
    return np.asarray(rec).reshape(-1).astype(float)


def audio_correlation_test(
    device: Device,
    center_hz: int,
    *,
    sample_rate: int = DEFAULT_IQ_RATE,
    duration_s: float = DEFAULT_DURATION_S,
    audio_fs: int = DEFAULT_AUDIO_FS,
    play_record=play_and_record,
) -> float:
    """Play a chirp, capture IQ at center_hz concurrently, return 0..1 correlation.

    Correlates the RF envelope (best of AM/FM) against the recorded room audio,
    tolerant to playback/capture latency.
    """
    chirp = make_chirp(duration_s, audio_fs)
    n_iq = int(sample_rate * duration_s)

    captured: dict[str, np.ndarray] = {}

    def _capture() -> None:
        captured["iq"] = device.capture_iq(int(center_hz), sample_rate, n_iq)

    thread = threading.Thread(target=_capture)
    thread.start()
    mic = play_record(chirp, audio_fs)          # blocks ~duration_s, runs with capture
    thread.join()

    iq = captured.get("iq", np.zeros(0, dtype=np.complex64))

    # Bring the recorded room audio and both RF envelopes to a common rate.
    ref = resample_to(mic, max(int(len(mic) * ENVELOPE_FS / audio_fs), 1))
    am = iq_to_am_envelope(iq, sample_rate, ENVELOPE_FS)
    fm = iq_to_fm_envelope(iq, sample_rate, ENVELOPE_FS)
    max_lag = int(MAX_LAG_S * ENVELOPE_FS)

    return max(
        best_lag_correlation(am, ref, max_lag),
        best_lag_correlation(fm, ref, max_lag),
    )
