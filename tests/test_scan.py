from quietroom.radio.recorded import demo_device
from quietroom.scan import capture_baseline, live_findings


def test_capture_baseline_then_find_planted_bug():
    dev = demo_device()
    baseline = capture_baseline(dev, 300_000_000, 320_000_000, 1_000_000, cycles=3)
    assert baseline.sweep_count == 3
    findings = live_findings(dev, baseline, dev.live_sweep())
    assert findings
    assert findings[0].score > 50.0
    # 1 MHz sweep bins => not "narrowband" (<=50 kHz); it scores via the
    # unknown-band + near-field + covert-band signals instead.
    assert "not in any known band" in findings[0].reasons
