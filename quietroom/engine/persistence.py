"""Persistence across sweeps: separate steady emitters from transients.

A covert transmitter is usually on continuously; a single-sweep spike is often
bursty traffic (a WiFi beacon, a phone checking in). The tracker remembers
which frequencies were flagged in the last `window` sweeps and annotates each
new finding with how often it has been seen.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace

from quietroom.engine.score import squash
from quietroom.engine.spectrum import Finding

PERSIST_MAX_POINTS = 10.0


def _tolerance(f: Finding, tolerance_hz: float | None) -> float:
    if tolerance_hz is not None:
        return tolerance_hz
    # Match within 1.5 sweep bins so a carrier that straddles bins still matches.
    return 1.5 * float(getattr(f.candidate, "bin_hz", 0.0) or f.candidate.bandwidth_hz or 0.0)


class PersistenceTracker:
    def __init__(self, window: int = 5, min_hits: int = 2,
                 tolerance_hz: float | None = None) -> None:
        if window < 1 or not (1 <= min_hits <= window):
            raise ValueError("need window >= 1 and 1 <= min_hits <= window")
        self.window = window
        self.min_hits = min_hits
        self.tolerance_hz = tolerance_hz
        self._history: deque[list[float]] = deque(maxlen=window)
        self.sweeps_seen = 0

    def reset(self) -> None:
        self._history.clear()
        self.sweeps_seen = 0

    def update(self, findings: list[Finding]) -> list[Finding]:
        """Record one sweep's findings and return them annotated with persistence."""
        self._history.append([f.candidate.center_freq_hz for f in findings])
        self.sweeps_seen += 1
        n = len(self._history)
        out = []
        for f in findings:
            tol = _tolerance(f, self.tolerance_hz)
            freq = f.candidate.center_freq_hz
            hits = sum(
                1 for sweep in self._history
                if any(abs(freq - g) <= tol for g in sweep)
            )
            out.append(_annotate(f, hits, n, self.min_hits))
        return out


def _annotate(f: Finding, hits: int, n: int, min_hits: int) -> Finding:
    breakdown = dict(f.breakdown)
    reasons = list(f.reasons)
    raw = f.raw_score
    if hits >= min_hits:
        bonus = PERSIST_MAX_POINTS * hits / n
        breakdown["persistence"] = bonus
        reasons.append(f"persistent: seen in {hits}/{n} sweeps")
        return replace(f, score=squash(raw + bonus), raw_score=raw + bonus,
                       reasons=reasons, breakdown=breakdown)
    breakdown["persistence"] = 0.0
    reasons.append(f"unconfirmed: seen in {hits}/{n} sweeps")
    return replace(f, reasons=reasons, breakdown=breakdown)


def confirm_findings(batches: list[list[Finding]], min_hits: int = 2,
                     tolerance_hz: float | None = None) -> list[Finding]:
    """Combine several sweeps' findings; keep emitters seen in >= min_hits sweeps.

    Each kept emitter is reported from the latest sweep it appeared in,
    annotated with its hit count over the whole batch, sorted by score.
    """
    n = len(batches)
    if not (1 <= min_hits <= max(n, 1)):
        raise ValueError("need 1 <= min_hits <= number of sweeps")
    latest: list[Finding] = []
    for batch in batches:
        for f in batch:
            tol = _tolerance(f, tolerance_hz)
            freq = f.candidate.center_freq_hz
            latest = [g for g in latest
                      if abs(g.candidate.center_freq_hz - freq) > tol]
            latest.append(f)
    final = []
    for f in latest:
        tol = _tolerance(f, tolerance_hz)
        freq = f.candidate.center_freq_hz
        hits = sum(1 for b in batches
                   if any(abs(g.candidate.center_freq_hz - freq) <= tol for g in b))
        if hits >= min_hits:
            final.append(_annotate(f, hits, n, min_hits))
    final.sort(key=lambda f: f.score, reverse=True)
    return final
