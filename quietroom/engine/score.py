"""Fuse detector outputs into a 0-100 suspicion score with reasons."""
from __future__ import annotations

import math

from quietroom.bands import BANDS, Band, label_for
from quietroom.engine.spectrum import Candidate, DetectorResult, Finding

# Raw SNR over baseline contributes a small, capped amount on its own.
SNR_CAP_DB = 20.0
# Saturation scale for squash(): raw evidence of SCALE points maps to ~63/100.
# Chosen so a strong unexplained emitter (~90 raw) lands near 80, and an
# audio-confirmed one (~140 raw) above 90, without ever pinning at 100.
SCALE = 55.0


def squash(raw: float) -> float:
    """Map unbounded raw evidence to 0..100 monotonically (never reaches 100)."""
    return 100.0 * (1.0 - math.exp(-max(raw, 0.0) / SCALE))


def unsquash(score: float) -> float:
    """Inverse of squash for 0 <= score < 100."""
    return -SCALE * math.log(1.0 - min(max(score, 0.0), 99.999999) / 100.0)


def score_candidate(
    candidate: Candidate,
    results: list[DetectorResult],
    bands: list[Band] = BANDS,
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

    score = squash(total)
    band = label_for(candidate.center_freq_hz, bands)
    band_label = band.label if band else "unknown"

    return Finding(
        candidate=candidate,
        score=score,
        band_label=band_label,
        reasons=reasons,
        breakdown=breakdown,
        raw_score=float(total),
    )
