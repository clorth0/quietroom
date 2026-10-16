"""Orchestrate the engine: baseline + live sweep -> ranked findings."""
from __future__ import annotations

from quietroom.bands import BANDS, Band

from quietroom.engine.detectors.catalog import score_band
from quietroom.engine.detectors.diff import find_candidates
from quietroom.engine.detectors.signatures import score_signatures
from quietroom.engine.plan import SweepPlan
from quietroom.engine.score import score_candidate
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum


def _spacing_hz(freqs) -> float:
    values = [float(f) for f in freqs]
    if len(values) < 2:
        raise ValueError("spectrum needs at least two bins to check against the plan")
    step = values[1] - values[0]
    for index in range(1, len(values)):
        if abs((values[index] - values[index - 1]) - step) > 1.0:
            raise ValueError("spectrum bin spacing is not uniform")
    return step


def _require_spacing(freqs, plan: SweepPlan) -> None:
    spacing = _spacing_hz(freqs)
    if abs(spacing - plan.bin_hz) > 1.0:
        raise ValueError(
            f"spectrum bin spacing {spacing:.0f} Hz does not match "
            f"the plan bin {plan.bin_hz} Hz"
        )


def sweep_findings(
    live: PowerSpectrum,
    baseline: Baseline,
    plan: SweepPlan,
    k: float = 4.0,
    bands: list[Band] = BANDS,
) -> list[Finding]:
    """Run the diff + catalog + signature detectors and rank the results.

    The audio-correlation detector is intentionally not run here: it requires
    tuning and microphone capture and is triggered on demand per candidate by
    the radio/web layer, which then re-scores that candidate with an extra
    audio DetectorResult.
    """
    _require_spacing(live.freqs_hz, plan)
    _require_spacing(baseline.freqs_hz, plan)
    findings: list[Finding] = []
    for candidate in find_candidates(live, baseline, k=k):
        results = [score_band(candidate, bands), score_signatures(candidate)]
        findings.append(score_candidate(candidate, results, bands))
    findings.sort(key=lambda f: f.score, reverse=True)
    return findings
