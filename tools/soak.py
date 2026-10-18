"""Soak-test scaffolding: simulated long-run monitoring without hardware.

Runs the real scan pipeline against the streaming demo device for N ticks,
counting findings/errors and writing a JSON summary. On hardware, set
QUIETROOM_HW=1 and use tests/hardware (self-hosted runner) — this simulated
soak validates the software loop, not RF performance.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.radio.recorded import streaming_demo_device
from quietroom.scan import capture_baseline


def run_soak(ticks=100, out=None, device=None):
    device = device or streaming_demo_device(n_clean=3)
    plan = SweepPlan.search()
    baseline = capture_baseline(device, plan, cycles=3)
    findings = errors = 0
    started = time.time()
    for _ in range(ticks):
        try:
            spectrum = next(iter(device.sweep(plan, cycles=1)))
            findings += len(sweep_findings(spectrum, baseline, plan))
        except Exception:
            errors += 1
    result = {"ticks": ticks, "findings": findings, "errors": errors,
              "seconds": round(time.time() - started, 3), "simulated": True}
    if out:
        Path(out).write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    print(run_soak(ticks=n, out="soak-summary.json"))
