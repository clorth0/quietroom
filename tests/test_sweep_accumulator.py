import math

import numpy as np

from quietroom.radio.parse import SweepAccumulator, DEAD_BIN_DBM


def test_accumulates_segments_into_one_sweep_on_cycle_boundary():
    acc = SweepAccumulator()
    # First cycle: two segments arriving out of order. No flush yet.
    assert acc.add(200_000_000, 1_000_000.0, [-70.0], timestamp=1.0) is None
    assert acc.add(100_000_000, 1_000_000.0, [-90.0], timestamp=1.0) is None
    # Repeat of an hz_low (100 MHz) marks a new cycle -> flush the first cycle.
    ps = acc.add(100_000_000, 1_000_000.0, [-91.0], timestamp=2.0)
    assert ps is not None
    # Frequencies are sorted ascending: 100 MHz bin then 200 MHz bin.
    assert ps.freqs_hz[0] < ps.freqs_hz[1]
    assert ps.power_dbm[0] == -90.0   # the 100 MHz segment
    assert ps.power_dbm[1] == -70.0   # the 200 MHz segment
    assert ps.timestamp == 2.0


def test_non_finite_powers_are_floored():
    acc = SweepAccumulator()
    acc.add(100_000_000, 1_000_000.0, [float("nan")], timestamp=1.0)
    ps = acc.add(100_000_000, 1_000_000.0, [-90.0], timestamp=2.0)  # flush
    assert math.isfinite(ps.power_dbm[0])
    assert ps.power_dbm[0] == DEAD_BIN_DBM
