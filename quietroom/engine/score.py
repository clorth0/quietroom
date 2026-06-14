"""Fuse detector outputs into a 0-100 suspicion score with reasons."""
from __future__ import annotations

from quietroom.bands import label_for
from quietroom.engine.spectrum import Candidate, DetectorResult, Finding

# Raw SNR over baseline contributes a small, capped amount on its own.
SNR_CAP_DB = 20.0


def score_candidate(
    candidate: Candidate,
    results: list[DetectorResult],
) -> Finding:
    breakdown: dict[str, float] = {}
    reasons: list[str] = []

    snr_term = min(max(candidate.snr_over_baseline_db, 0.0), SNR_CAP_DB)
    total = snr_term
    breakdown["snr"] = snr_term

    for r in results:
        total += r.contribution
        breakdown[r.name] = r.contribution
        reasons.extend(r.reasons)

    score = float(max(0.0, min(100.0, total)))
    band = label_for(candidate.center_freq_hz)
    band_label = band.label if band else "unknown"

    return Finding(
        candidate=candidate,
        score=score,
        band_label=band_label,
        reasons=reasons,
        breakdown=breakdown,
    )
