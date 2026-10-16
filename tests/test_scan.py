from quietroom.engine.plan import SweepPlan
from quietroom.radio.recorded import demo_device
from quietroom.scan import capture_baseline, live_findings


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
