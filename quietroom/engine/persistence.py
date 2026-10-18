"""Persistence across sweeps: separate steady emitters from transients.

A covert transmitter is usually on continuously; a single-sweep spike is often
bursty traffic (a WiFi beacon, a phone checking in). The tracker remembers
which frequencies were flagged in the last `window` sweeps and annotates each
new finding with how often it has been seen.

This is a deep module: hit counting, frequency tolerance and the
confirmation threshold live in exactly one implementation, ``count_hits``,
behind a small interface. Both call shapes -- the streaming tracker and the
batch combiner -- cross that seam, so a matching fix is made (and tested)
once. What differs between the callers is policy, and policy stays at the
call sites, intentionally:

- ``PersistenceTracker.update`` (web) keeps every finding and lets
  ``apply_persistence`` mark below-threshold ones "unconfirmed";
- ``confirm_findings`` (CLI batch) drops findings below ``min_hits``.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from quietroom.engine.score import PERSIST_MAX_POINTS as PERSIST_MAX_POINTS
from quietroom.engine.score import apply_persistence
from quietroom.engine.spectrum import Finding


def _tolerance(f: Finding, tolerance_hz: float | None) -> float:
    if tolerance_hz is not None:
        return tolerance_hz
    # Match within 1.5 sweep bins so a carrier that straddles bins still matches.
    return 1.5 * float(getattr(f.candidate, "bin_hz", 0.0) or f.candidate.bandwidth_hz or 0.0)


@dataclass(frozen=True)
class HitCount:
    """The confirmation facts for one finding over a set of sweeps."""

    hits: int
    sweeps: int
    min_hits: int

    @property
    def confirmed(self) -> bool:
        return self.hits >= self.min_hits


def count_hits(finding: Finding, sweeps: Iterable[Iterable[float]],
               min_hits: int, tolerance_hz: float | None = None) -> HitCount:
    """Count, in one implementation, in how many sweeps `finding` was seen.

    `sweeps` holds the sighted frequencies per sweep. The counting rules
    live here only: a sweep counts once however many sightings in it match
    (duplicate sightings in one sweep are one hit), a sighting matches when
    it is within the finding's tolerance (inclusive), and the finding is
    confirmed when `hits` reaches `min_hits` (exactly-at-threshold counts).
    Callers decide what confirmation means -- keep-and-mark or drop -- at
    their own call site; this function encodes no keep/drop policy.
    """
    tol = _tolerance(finding, tolerance_hz)
    freq = finding.candidate.center_freq_hz
    sweep_list = list(sweeps)
    hits = sum(
        1 for sweep in sweep_list
        if any(abs(freq - g) <= tol for g in sweep)
    )
    return HitCount(hits=hits, sweeps=len(sweep_list), min_hits=min_hits)


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

    def count_for(self, finding: Finding) -> HitCount:
        """Hit count for `finding` over the sweeps recorded so far.

        Read-only: an investigate re-score asks this so it can carry the
        annotation forward without recording a new sweep (investigating
        is not a sweep, and counting it as one would inflate the count).
        """
        return count_hits(finding, self._history, self.min_hits, self.tolerance_hz)

    def update(self, findings: list[Finding]) -> list[Finding]:
        """Record one sweep's findings and return them annotated with persistence."""
        self._history.append([f.candidate.center_freq_hz for f in findings])
        self.sweeps_seen += 1
        out = []
        for f in findings:
            count = self.count_for(f)
            # Policy (streaming/web): keep every finding; apply_persistence
            # marks ones below min_hits "unconfirmed" instead of dropping them.
            out.append(apply_persistence(f, count.hits, count.sweeps, self.min_hits))
        return out


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
    sweep_freqs = [[g.candidate.center_freq_hz for g in b] for b in batches]
    final = []
    for f in latest:
        count = count_hits(f, sweep_freqs, min_hits, tolerance_hz)
        # Policy (batch/CLI): drop transients below min_hits; the streaming
        # tracker's keep-and-mark-unconfirmed policy is deliberately not
        # unified with this one.
        if count.confirmed:
            final.append(apply_persistence(f, count.hits, count.sweeps, min_hits))
    final.sort(key=lambda f: f.score, reverse=True)
    return final
