"""Catalog of expected/licensed RF bands for known-emitter subtraction."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    start_hz: float
    stop_hz: float
    label: str


# Expected/licensed bands. Membership means a signal is "explained".
# Deliberately omits 380 MHz so tests have a known unallocated gap.
BANDS: list[Band] = [
    Band(530e3, 1_700e3, "AM broadcast"),
    Band(88e6, 108e6, "FM broadcast"),
    Band(118e6, 137e6, "Airband"),
    Band(174e6, 216e6, "VHF TV / DAB"),
    Band(470e6, 698e6, "UHF TV"),
    Band(824e6, 894e6, "Cellular 850"),
    Band(902e6, 928e6, "ISM 915"),
    Band(1_710e6, 1_780e6, "Cellular 1700"),
    Band(1_850e6, 1_990e6, "Cellular 1900"),
    Band(2_400e6, 2_483.5e6, "WiFi/BLE 2.4 GHz ISM"),
    Band(5_150e6, 5_850e6, "WiFi 5 GHz"),
]


def label_for(freq_hz: float, bands: list[Band] = BANDS) -> Band | None:
    """Return the catalogued band containing freq_hz, or None."""
    for band in bands:
        if band.start_hz <= freq_hz <= band.stop_hz:
            return band
    return None
