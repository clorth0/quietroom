"""Known-emitter subtraction: explained bands lower suspicion.

Broadcast bands strongly explain a signal. Shared consumer/ISM/cellular bands
only weakly explain one, and a near-field emitter in a shared band is not
discounted at all: that is exactly what a WiFi/BLE/GSM bug looks like.
"""
from __future__ import annotations

from quietroom.bands import Band, BANDS, label_for
from quietroom.engine.detectors.signatures import NEARFIELD_DBM
from quietroom.engine.spectrum import Candidate, DetectorResult

UNKNOWN_BAND_POINTS = 30.0
KNOWN_BAND_POINTS = -40.0          # broadcast bands
SHARED_BAND_POINTS = -10.0         # shared bands, far-field signal
SHARED_NEARFIELD_POINTS = 0.0      # shared bands, near-field signal


def score_band(candidate: Candidate, bands: list[Band] = BANDS) -> DetectorResult:
    band = label_for(candidate.center_freq_hz, bands)
    if band is None:
        return DetectorResult("catalog", UNKNOWN_BAND_POINTS,
                              ["not in any known band"])
    if band.kind == "shared":
        if candidate.peak_power_dbm >= NEARFIELD_DBM:
            return DetectorResult(
                "catalog", SHARED_NEARFIELD_POINTS,
                [f"strong emitter in shared {band.label} band "
                 "(covert devices hide here)"],
            )
        return DetectorResult("catalog", SHARED_BAND_POINTS,
                              [f"in shared {band.label} band"])
    return DetectorResult("catalog", KNOWN_BAND_POINTS,
                          [f"in known {band.label} band"])
