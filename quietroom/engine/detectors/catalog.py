"""Known-emitter subtraction: explained bands lower suspicion."""
from __future__ import annotations

from quietroom.bands import Band, BANDS, label_for
from quietroom.engine.spectrum import Candidate, DetectorResult

UNKNOWN_BAND_POINTS = 30.0
KNOWN_BAND_POINTS = -40.0


def score_band(candidate: Candidate, bands: list[Band] = BANDS) -> DetectorResult:
    band = label_for(candidate.center_freq_hz, bands)
    if band is None:
        return DetectorResult("catalog", UNKNOWN_BAND_POINTS,
                              ["not in any known band"])
    return DetectorResult("catalog", KNOWN_BAND_POINTS,
                          [f"in known {band.label} band"])
