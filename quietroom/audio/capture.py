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
