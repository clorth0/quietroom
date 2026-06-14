"""Core data types for the detection engine."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class PowerSpectrum:
    """One sweep result: power versus frequency at a moment in time."""
    freqs_hz: np.ndarray
    power_dbm: np.ndarray
    timestamp: float


@dataclass
class Baseline:
    """A learned clean RF profile: per-bin mean and standard deviation."""
    freqs_hz: np.ndarray
    mean_dbm: np.ndarray
    std_dbm: np.ndarray
    sweep_count: int
    created_at: float


@dataclass
class Candidate:
    """A peak worth investigating, found by the diff detector."""
    center_freq_hz: float
    bandwidth_hz: float
    peak_power_dbm: float
    snr_over_baseline_db: float


@dataclass
class DetectorResult:
    """One detector's signed contribution to the suspicion score."""
    name: str
    contribution: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """A scored, ranked candidate with human-readable reasons."""
    candidate: Candidate
    score: float
    band_label: str
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)
