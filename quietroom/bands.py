"""Catalog of expected/licensed RF bands for known-emitter subtraction."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    start_hz: float
    stop_hz: float
    label: str
    # "broadcast": high-power, fixed, licensed services (AM/FM/TV/airband).
    #   A signal here is very likely explained.
    # "shared": consumer/ISM/cellular bands where covert transmitters
    #   (WiFi/BLE/GSM/LTE bugs, 433 MHz trackers) routinely hide. Membership
    #   only weakly explains a signal.
    kind: str = "broadcast"


# US allocations (default). Membership means a signal is at least partly
# "explained". Deliberately omits 380 MHz so tests have a known unallocated gap.
US_BANDS: list[Band] = [
    Band(530e3, 1_700e3, "AM broadcast"),
    Band(88e6, 108e6, "FM broadcast"),
    Band(118e6, 137e6, "Airband"),
    Band(174e6, 216e6, "VHF TV / DAB"),
    Band(433.05e6, 434.79e6, "ISM 433", "shared"),
    Band(470e6, 698e6, "UHF TV / 600 MHz LTE"),
    Band(698e6, 806e6, "LTE 700", "shared"),
    Band(824e6, 894e6, "Cellular 850", "shared"),
    Band(902e6, 928e6, "ISM 915", "shared"),
    Band(1_710e6, 1_780e6, "Cellular 1700 (AWS uplink)", "shared"),
    Band(1_850e6, 1_995e6, "Cellular 1900 (PCS)", "shared"),
    Band(2_110e6, 2_200e6, "AWS downlink", "shared"),
    Band(2_400e6, 2_483.5e6, "WiFi/BLE 2.4 GHz ISM", "shared"),
    Band(2_496e6, 2_690e6, "2.5 GHz LTE/5G (BRS/EBS)", "shared"),
    Band(3_550e6, 3_700e6, "CBRS 3.5 GHz", "shared"),
    Band(5_150e6, 5_850e6, "WiFi 5 GHz", "shared"),
]

# EU / CEPT allocations.
EU_BANDS: list[Band] = [
    Band(530e3, 1_700e3, "AM broadcast"),
    Band(87.5e6, 108e6, "FM broadcast"),
    Band(118e6, 137e6, "Airband"),
    Band(174e6, 240e6, "DAB / VHF TV"),
    Band(433.05e6, 434.79e6, "ISM 433", "shared"),
    Band(470e6, 790e6, "UHF TV"),
    Band(791e6, 862e6, "LTE 800", "shared"),
    Band(863e6, 870e6, "SRD/ISM 868", "shared"),
    Band(880e6, 960e6, "GSM/LTE 900", "shared"),
    Band(1_710e6, 1_880e6, "GSM/LTE 1800", "shared"),
    Band(1_920e6, 2_170e6, "UMTS/LTE 2100", "shared"),
    Band(2_400e6, 2_483.5e6, "WiFi/BLE 2.4 GHz ISM", "shared"),
    Band(2_500e6, 2_690e6, "LTE 2600", "shared"),
    Band(3_400e6, 3_800e6, "5G 3.5 GHz", "shared"),
    Band(5_150e6, 5_850e6, "WiFi 5 GHz", "shared"),
]

REGIONS: dict[str, list[Band]] = {"us": US_BANDS, "eu": EU_BANDS}

# Backward-compatible default.
BANDS: list[Band] = US_BANDS


def bands_for(region: str) -> list[Band]:
    """Return the band catalog for a region code ("us", "eu")."""
    try:
        return REGIONS[region.lower()]
    except KeyError:
        raise ValueError(
            f"unknown region {region!r}; choose from {sorted(REGIONS)}"
        ) from None


def label_for(freq_hz: float, bands: list[Band] = BANDS) -> Band | None:
    """Return the catalogued band containing freq_hz, or None."""
    for band in bands:
        if band.start_hz <= freq_hz <= band.stop_hz:
            return band
    return None
