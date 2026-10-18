"""Scoring assembly: fuse detector outputs into a 0-100 suspicion score.

This is the one deep module behind both operator paths. It owns:

- the detector roster (``DETECTOR_ROSTER``) — a future detector is added
  here, in exactly one place, and then runs on fresh sweeps and on
  investigate re-scores alike;
- band labelling — the catalog band is resolved once per candidate and
  the same resolution feeds the catalog detector and ``Finding.band_label``;
- score fusion — the SNR term, detector contributions, breakdown keys and
  ordering, reasons, ``raw_score`` and the squash to 0..100, plus the
  persistence annotation that re-fuses a bonus into an existing finding.

Callers cross this seam through ``assemble_finding`` (fresh or
investigate scoring), ``band_label_for``, ``apply_persistence`` and, for
caller-supplied detector results, ``score_candidate``. Pipeline, scan and
persistence no longer assemble a roster or re-derive fusion themselves.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

from quietroom.bands import BANDS, Band, label_for
from quietroom.engine.detectors.audio_corr import score_audio
from quietroom.engine.detectors.catalog import score_band_in
from quietroom.engine.detectors.signatures import score_signatures
from quietroom.engine.spectrum import Candidate, DetectorResult, Finding

# Raw SNR over baseline contributes a small, capped amount on its own.
SNR_CAP_DB = 20.0
# Saturation scale for squash(): raw evidence of SCALE points maps to ~63/100.
# Chosen so a strong unexplained emitter (~90 raw) lands near 80, and an
# audio-confirmed one (~140 raw) above 90, without ever pinning at 100.
SCALE = 55.0
# Most points a steady emitter can earn for being seen again and again.
PERSIST_MAX_POINTS = 10.0

# One roster entry: a detector scored against a candidate whose catalog
# band has already been resolved by the assembly (None when uncatalogued).
RosterDetector = Callable[[Candidate, Band | None], DetectorResult]


def _catalog(candidate: Candidate, band: Band | None) -> DetectorResult:
    return score_band_in(candidate, band)


def _signatures(candidate: Candidate, band: Band | None) -> DetectorResult:
    return score_signatures(candidate)


# THE detector roster. Add a future detector here — and only here — and it
# runs on every path that crosses assemble_finding. Order is load-bearing:
# it sets breakdown key order and reason order.
DETECTOR_ROSTER: tuple[RosterDetector, ...] = (_catalog, _signatures)


def squash(raw: float) -> float:
    """Map unbounded raw evidence to 0..100 monotonically (never reaches 100)."""
    return 100.0 * (1.0 - math.exp(-max(raw, 0.0) / SCALE))


def unsquash(score: float) -> float:
    """Inverse of squash for 0 <= score < 100."""
    return -SCALE * math.log(1.0 - min(max(score, 0.0), 99.999999) / 100.0)


def band_label_for(freq_hz: float, bands: list[Band] = BANDS) -> str:
    """The one band labelling: catalog label, or "unknown" when uncatalogued."""
    band = label_for(freq_hz, bands)
    return band.label if band else "unknown"


def _fuse(candidate: Candidate, results: list[DetectorResult],
          band_label: str) -> Finding:
    breakdown: dict[str, float] = {}
    reasons: list[str] = []

    snr_term = min(max(candidate.snr_over_baseline_db, 0.0), SNR_CAP_DB)
    total = snr_term
    breakdown["snr"] = snr_term

    for r in results:
        total += r.contribution
        breakdown[r.name] = r.contribution
        reasons.extend(r.reasons)

    return Finding(
        candidate=candidate,
        score=squash(total),
        band_label=band_label,
        reasons=reasons,
        breakdown=breakdown,
        raw_score=float(total),
    )


def score_candidate(
    candidate: Candidate,
    results: list[DetectorResult],
    bands: list[Band] = BANDS,
) -> Finding:
    """Fuse caller-supplied detector results for a candidate into a Finding."""
    return _fuse(candidate, results, band_label_for(candidate.center_freq_hz, bands))


def assemble_finding(
    candidate: Candidate,
    bands: list[Band] = BANDS,
    *,
    audio_correlation: float | None = None,
) -> Finding:
    """Score a candidate through the one roster, labelling its band once.

    The audio-correlation detector is not in the roster: it needs tuning
    and microphone capture, so it is run on demand by the caller and its
    correlation is added through this same seam when present.
    """
    band = label_for(candidate.center_freq_hz, bands)
    results = [detector(candidate, band) for detector in DETECTOR_ROSTER]
    if audio_correlation is not None:
        results.append(score_audio(audio_correlation))
    return _fuse(candidate, results, band.label if band else "unknown")


def apply_persistence(finding: Finding, hits: int, sweeps: int,
                      min_hits: int) -> Finding:
    """Annotate a finding with its persistence across sweeps.

    The fusion edit lives here, not in the caller: a confirmed emitter's
    bonus joins the breakdown and reasons and is re-squashed into the
    score; an unconfirmed one records a zero persistence entry instead.
    """
    breakdown = dict(finding.breakdown)
    reasons = list(finding.reasons)
    raw = finding.raw_score
    if hits >= min_hits:
        bonus = PERSIST_MAX_POINTS * hits / sweeps
        breakdown["persistence"] = bonus
        reasons.append(f"persistent: seen in {hits}/{sweeps} sweeps")
        return replace(finding, score=squash(raw + bonus), raw_score=raw + bonus,
                       reasons=reasons, breakdown=breakdown)
    breakdown["persistence"] = 0.0
    reasons.append(f"unconfirmed: seen in {hits}/{sweeps} sweeps")
    return replace(finding, reasons=reasons, breakdown=breakdown)
