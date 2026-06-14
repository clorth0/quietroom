"""Audio-correlation detector: does an RF envelope move with room audio?"""
from __future__ import annotations

import numpy as np

from quietroom.engine.spectrum import DetectorResult

AUDIO_CORR_THRESHOLD = 0.5
AUDIO_POINTS = 50.0


def envelope_correlation(rf_envelope: np.ndarray, audio_ref: np.ndarray) -> float:
    """Normalized cross-correlation magnitude (0..1) of two aligned signals."""
    a = np.asarray(rf_envelope, dtype=float)
    b = np.asarray(audio_ref, dtype=float)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    denom = np.sqrt(np.sum(a * a) * np.sum(b * b))
    if denom == 0:
        return 0.0
    return float(abs(np.dot(a, b)) / denom)


def score_audio(correlation: float) -> DetectorResult:
    if correlation >= AUDIO_CORR_THRESHOLD:
        return DetectorResult(
            "audio", AUDIO_POINTS,
            [f"envelope correlates with room audio at {correlation:.2f}"],
        )
    return DetectorResult("audio", 0.0, [])
