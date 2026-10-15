"""Bug-signature heuristics: telltale shapes and bands of covert transmitters."""
from __future__ import annotations

from quietroom.engine.spectrum import Candidate, DetectorResult

NARROWBAND_HZ = 50_000.0
NEARFIELD_DBM = -40.0
NARROWBAND_POINTS = 15.0
NEARFIELD_POINTS = 15.0
COVERT_POINTS = 20.0
# A single hot bin wider than NARROWBAND_HZ: cannot prove narrowband at this
# resolution, but it is unresolved, so it earns partial credit.
UNRESOLVED_POINTS = 7.0

# (start_hz, stop_hz, label) bands disproportionately used by covert transmitters.
COVERT_BANDS = [
    (300e6, 470e6, "common analog bug band"),
    (1_200e6, 1_300e6, "1.2 GHz analog video band"),
]


def score_signatures(candidate: Candidate) -> DetectorResult:
    contribution = 0.0
    reasons: list[str] = []

    if candidate.bandwidth_hz <= NARROWBAND_HZ:
        contribution += NARROWBAND_POINTS
        reasons.append("narrowband carrier")
    elif candidate.n_bins == 1 and candidate.bin_hz > 0:
        contribution += UNRESOLVED_POINTS
        reasons.append(
            f"unresolved single-bin signal (<= {candidate.bandwidth_hz / 1e3:.0f} kHz); "
            "rescan with a finer --bin to confirm narrowband"
        )

    if candidate.peak_power_dbm >= NEARFIELD_DBM:
        contribution += NEARFIELD_POINTS
        reasons.append("near-field strong signal")

    for start, stop, label in COVERT_BANDS:
        if start <= candidate.center_freq_hz <= stop:
            contribution += COVERT_POINTS
            reasons.append(f"in {label}")
            break

    return DetectorResult("signatures", contribution, reasons)
