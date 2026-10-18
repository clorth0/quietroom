"""HackRF radio limits shared by the sweep plan and the HackRF adapter.

This leaf module owns the constants so ``engine.plan`` and
``radio.hackrf`` can both depend on them without either depending on
the other: that is what lets ``radio.device`` import ``SweepPlan``
for the Device interface without a plan <-> hackrf import cycle.
"""
from __future__ import annotations

MIN_HZ = 1_000_000
MAX_HZ = 6_000_000_000
# Default sweep resolution. hackrf_sweep's firmware captures a fixed block of
# samples at each 10 MHz step whatever -w is, so a finer bin costs little sweep
# time; it only grows the host FFT and output. 25 kHz resolves 12.5/25 kHz NFM
# bugs (<= the 50 kHz narrowband threshold) and lowers per-bin noise ~16 dB vs
# 1 MHz, so narrow carriers stand out.
DEFAULT_BIN_HZ = 25_000
