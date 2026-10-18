"""Orchestrate the engine: baseline + live sweep -> ranked findings."""
from __future__ import annotations

from quietroom.bands import BANDS, Band

from quietroom.engine.detectors.diff import find_candidates
from quietroom.engine.plan import SweepPlan
from quietroom.engine.score import assemble_finding
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum


def sweep_findings(
    live: PowerSpectrum,
    baseline: Baseline,
    plan: SweepPlan,
    k: float = 4.0,
    bands: list[Band] = BANDS,
) -> list[Finding]:
    """Run the diff detector, score each candidate, and rank the results.

    Scoring itself — the detector roster, band labelling and fusion — is
    owned by the scoring assembly (engine.score); this module only finds
    candidates and hands each one across that seam. The audio-correlation
    detector is intentionally not run here: it requires tuning and
    microphone capture and is triggered on demand per candidate by the
    radio/web layer, which re-scores through the same seam.
    """
    live.grid.require_bin_hz(plan.bin_hz)
    baseline.grid.require_bin_hz(plan.bin_hz)
    findings: list[Finding] = []
    for candidate in find_candidates(live, baseline, k=k):
        findings.append(assemble_finding(candidate, bands))
    findings.sort(key=lambda f: f.score, reverse=True)
    return findings
