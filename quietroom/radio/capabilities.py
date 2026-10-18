"""Radio capability metadata (hardware abstraction v2).

Capabilities let schedulers and fleet code ask what a radio can actually do
instead of assuming HackRF numbers everywhere. Power values from sweep tools
are uncalibrated unless `calibrated` says otherwise.
"""

from __future__ import annotations
from dataclasses import dataclass
from quietroom.radio.device import DeviceError


@dataclass(frozen=True)
class RadioCapabilities:
    name: str
    min_hz: int
    max_hz: int
    max_sample_rate_hz: int = 2_400_000
    tx_capable: bool = False
    calibrated: bool = False
    notes: str = ""

    def covers(self, start_hz, stop_hz):
        return self.min_hz <= start_hz and stop_hz <= self.max_hz


def check_plan(caps, plan):
    if not caps.covers(plan.start_hz, plan.stop_hz):
        raise DeviceError(
            f"{caps.name} covers {caps.min_hz}-{caps.max_hz} Hz, "
            f"plan needs {plan.start_hz}-{plan.stop_hz} Hz"
        )
    return plan
