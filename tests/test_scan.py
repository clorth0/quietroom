import pytest

from quietroom.engine.plan import SweepPlan
from quietroom.radio.device import DeviceError
from quietroom.radio.recorded import DEMO_BUG_HZ, DEMO_FLOOR_DBM, demo_device, spectrum_grid
from quietroom.scan import capture_baseline, confirmed_findings, live_findings


def test_capture_baseline_then_find_planted_bug():
    dev = demo_device()
    plan = SweepPlan.search()
    baseline = capture_baseline(dev, plan.start_hz, plan.stop_hz, plan.bin_hz, cycles=3)
    assert baseline.sweep_count == 3
    findings = live_findings(
        dev, baseline, plan,
        dev.live_sweep(plan.start_hz, plan.stop_hz, plan.bin_hz),
    )
    assert findings
    assert findings[0].score > 50.0
    assert "not in any known band" in findings[0].reasons
    assert "narrowband carrier" in findings[0].reasons


def test_short_sweep_raises_instead_of_lowering_min_hits():
    dev = demo_device()
    plan = SweepPlan.search()
    baseline = capture_baseline(dev, plan.start_hz, plan.stop_hz, plan.bin_hz, cycles=3)
    hot = spectrum_grid(
        plan.start_hz, plan.stop_hz, plan.bin_hz, DEMO_FLOOR_DBM, 10.0, bug_hz=DEMO_BUG_HZ,
    )
    with pytest.raises(DeviceError, match="1 of 3"):
        confirmed_findings(dev, baseline, 3, 2, plan, live=iter([hot]))
