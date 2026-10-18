"""Resource-budget benchmark: synthetic hackrf_sweep lines -> parse + engine.

Run on the target board (Pi) before deployment; no radio needed. Reports
bins per sweep and wall time, so segment plans can be budgeted against the
revisit intervals in the node config.
"""

from __future__ import annotations
import time
from quietroom.engine.baseline import build_baseline
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.radio.parse import SweepAccumulator, parse_sweep_line


def _lines(f0, f1, binw):
    per = max(1, int(5e6 / binw))
    for lo in range(int(f0), int(f1), 5_000_000):
        yield f"d, t, {lo}, {lo + 5_000_000}, {binw:.2f}, 20, " + ", ".join(["-90.0"] * per)


def bench(f0=300_000_000, f1=320_000_000, binw=25_000, reps=3):
    lines = list(_lines(f0, f1, binw))
    acc = SweepAccumulator()
    sweeps = []
    t = time.perf_counter()
    for _ in range(reps + 1):
        for line in lines:
            r = acc.add(*parse_sweep_line(line), timestamp=0.0)
            if r is not None:
                sweeps.append(r)
    ms = (time.perf_counter() - t) / reps * 1000
    plan = SweepPlan.overview(f0, f1, binw)
    base = build_baseline(sweeps[:2])
    t = time.perf_counter()
    sweep_findings(sweeps[-1], base, plan)
    engine_ms = (time.perf_counter() - t) * 1000
    return {
        "bins": len(sweeps[0].freqs_hz),
        "ms_per_sweep": round(ms, 2),
        "engine_ms": round(engine_ms, 2),
    }


if __name__ == "__main__":
    for span, b in [
        ((300e6, 320e6), 25_000),
        ((300e6, 470e6), 25_000),
        ((1e6, 6000e6), 1_000_000),
        ((1e6, 6000e6), 25_000),
    ]:
        print(span, b, bench(int(span[0]), int(span[1]), b, reps=1))
