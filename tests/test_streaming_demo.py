import numpy as np

from quietroom.radio.recorded import streaming_demo_device
from quietroom.engine.baseline import build_baseline
from quietroom.engine.pipeline import sweep_findings


def test_baseline_clean_then_bug_appears():
    dev = streaming_demo_device(n_clean=3)
    baseline = build_baseline(list(dev.sweep(300_000_000, 320_000_000, 1_000_000, cycles=3)))
    # After the 3 clean baseline sweeps, the next sweep carries the planted bug.
    live = next(iter(dev.sweep(300_000_000, 320_000_000, 1_000_000, cycles=1)))
    findings = sweep_findings(live, baseline)
    assert findings and findings[0].score > 50.0
    assert abs(findings[0].candidate.center_freq_hz - 308_000_000.0) < 1e6


def test_clean_sweeps_have_no_bug():
    dev = streaming_demo_device(n_clean=3)
    first = next(iter(dev.sweep(0, 1, 1, cycles=1)))
    assert float(np.max(first.power_dbm)) < -80.0   # clean noise floor
