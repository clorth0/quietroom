from pathlib import Path

import numpy as np
import pytest

from quietroom.engine.baseline import build_baseline
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.engine.spectrum import (
    Baseline, Candidate, Finding, PowerSpectrum,
)
from quietroom.radio.recorded import demo_device

ROOT = Path(__file__).resolve().parents[1] / "quietroom"


def test_search_rejects_a_bin_wider_than_the_narrowband_threshold():
    with pytest.raises(ValueError, match="overview") as exc:
        SweepPlan.search(300_000_000, 320_000_000, 1_000_000)
    assert "50000" in str(exc.value)


def test_search_accepts_the_default_bin():
    plan = SweepPlan.search(300_000_000, 320_000_000, 25_000)
    assert plan.mode == "search"
    assert plan.bin_hz == 25_000


def test_overview_accepts_a_coarse_bin():
    plan = SweepPlan.overview(300_000_000, 320_000_000, 1_000_000)
    assert plan.mode == "overview"
    assert plan.bin_hz == 1_000_000


def test_default_search_is_25_khz():
    plan = SweepPlan.search()
    assert plan.bin_hz == 25_000
    assert plan.start_hz == 300_000_000
    assert plan.stop_hz == 320_000_000


def test_pipeline_rejects_spacing_that_does_not_match_the_plan():
    plan = SweepPlan.search()
    freqs = np.arange(plan.start_hz, plan.stop_hz, 1_000_000, dtype=float)
    power = np.full(len(freqs), -95.0)
    live = PowerSpectrum(freqs, power, 1.0)
    baseline = Baseline(freqs, power, np.full(len(freqs), 2.0), 1, 0.0)
    with pytest.raises(ValueError, match="does not match"):
        sweep_findings(live, baseline, plan)


def test_demo_grid_uses_the_requested_bin():
    plan = SweepPlan.search()
    spec = next(iter(demo_device().sweep(plan, cycles=1)))
    assert spec.freqs_hz[1] - spec.freqs_hz[0] == plan.bin_hz


def test_finding_requires_raw_score():
    candidate = Candidate(308e6, 25e3, -30.0, 20.0)
    with pytest.raises(TypeError):
        Finding(candidate=candidate, score=80.0, band_label="unknown")


def test_span_literals_live_only_in_the_plan():
    for path in ROOT.rglob("*.py"):
        text = path.read_text()
        if path.name == "plan.py":
            continue
        assert "300_000_000" not in text
        assert "320_000_000" not in text


def test_radio_limits_are_assigned_only_in_radio_limits():
    for path in ROOT.rglob("*.py"):
        if path.name == "limits.py":
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("MIN_HZ") or stripped.startswith("MAX_HZ"):
                if "=" in stripped and "import" not in stripped:
                    raise AssertionError(f"{path.name}: {stripped}")


def test_matched_spacing_still_scores():
    plan = SweepPlan.search(300_000_000, 300_200_000, 10_000)
    freqs = np.arange(plan.start_hz, plan.stop_hz, plan.bin_hz, dtype=float)
    quiet = np.full(len(freqs), -95.0)
    base = build_baseline([
        PowerSpectrum(freqs, quiet, 0.0),
        PowerSpectrum(freqs, quiet, 1.0),
        PowerSpectrum(freqs, quiet, 2.0),
    ])
    live_power = quiet.copy()
    live_power[3] = -30.0
    findings = sweep_findings(PowerSpectrum(freqs, live_power, 3.0), base, plan)
    assert findings
    assert findings[0].raw_score > 0
