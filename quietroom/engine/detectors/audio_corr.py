"""Audio-correlation detector: does an RF envelope move with room audio?"""
from __future__ import annotations

from quietroom.engine.spectrum import DetectorResult

AUDIO_CORR_THRESHOLD = 0.5
AUDIO_POINTS = 50.0


def score_audio(correlation: float) -> DetectorResult:
    if correlation >= AUDIO_CORR_THRESHOLD:
        return DetectorResult(
            "audio", AUDIO_POINTS,
            [f"envelope correlates with room audio at {correlation:.2f}"],
        )
    return DetectorResult("audio", 0.0, [])
