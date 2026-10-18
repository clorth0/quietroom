"""Known-device policy: allowlist suppresses, blocklist escalates.

Entries are explicit and human-authored (frequency range + optional segment).
Nothing here auto-allowlists: suggestions from operator feedback are a
central/fleet concern (item 5/8), not silent learning.
"""

from __future__ import annotations
from dataclasses import dataclass

ALLOW_ADJUST = -60.0
BLOCK_ADJUST = 30.0


@dataclass(frozen=True)
class AllowEntry:
    start_hz: float
    stop_hz: float
    label: str = "allowed"
    segment: str | None = None


@dataclass(frozen=True)
class BlockEntry:
    start_hz: float
    stop_hz: float
    label: str = "blocked"
    segment: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    adjustment: float
    reason: str


def _hit(e, freq, segment):
    return e.start_hz <= freq <= e.stop_hz and (e.segment is None or e.segment == segment)


class Policy:
    def __init__(self, allow=(), block=()):
        self.allow = list(allow)
        self.block = list(block)


def apply_policy(candidate, policy, segment=None):
    freq = candidate.center_freq_hz
    for e in policy.block:
        if _hit(e, freq, segment):
            return PolicyDecision("block", BLOCK_ADJUST, f"matches blocklist: {e.label}")
    for e in policy.allow:
        if _hit(e, freq, segment):
            return PolicyDecision("allow", ALLOW_ADJUST, f"matches allowlist: {e.label}")
    return PolicyDecision("none", 0.0, "")
