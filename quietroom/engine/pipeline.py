"""Orchestrate the engine: baseline + live sweep -> ranked findings."""
from __future__ import annotations

from quietroom.engine.detectors.catalog import score_band
from quietroom.engine.detectors.diff import find_candidates
from quietroom.engine.detectors.signatures import score_signatures
from quietroom.engine.score import score_candidate
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum


def sweep_findings(
    live: PowerSpectrum,
    baseline: Baseline,
    k: float = 4.0,
) -> list[Finding]:
    """Run the diff + catalog + signature detectors and rank the results.

    The audio-correlation detector is intentionally not run here: it requires
    tuning and microphone capture and is triggered on demand per candidate by
    the radio/web layer, which then re-scores that candidate with an extra
    audio DetectorResult.
    """
    findings: list[Finding] = []
    for candidate in find_candidates(live, baseline, k=k):
        results = [score_band(candidate), score_signatures(candidate)]
        findings.append(score_candidate(candidate, results))
    findings.sort(key=lambda f: f.score, reverse=True)
    return findings
