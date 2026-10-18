"""One sweep request shared by the CLI, the web session, and the pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from quietroom.engine.detectors.signatures import NARROWBAND_HZ
from quietroom.radio.limits import DEFAULT_BIN_HZ, MAX_HZ, MIN_HZ

DEFAULT_START_HZ = 300_000_000
DEFAULT_STOP_HZ = 320_000_000


@dataclass(frozen=True)
class SweepPlan:
    start_hz: int
    stop_hz: int
    bin_hz: int
    mode: Literal["search", "overview"]

    def __post_init__(self) -> None:
        if self.mode not in {"search", "overview"}:
            raise ValueError("mode must be 'search' or 'overview'")
        if not (MIN_HZ <= self.start_hz < self.stop_hz <= MAX_HZ):
            raise ValueError(
                f"frequency range {self.start_hz}-{self.stop_hz} Hz outside "
                f"{MIN_HZ}-{MAX_HZ} Hz"
            )
        if self.bin_hz <= 0:
            raise ValueError("bin_hz must be positive")
        if self.stop_hz - self.start_hz < self.bin_hz:
            raise ValueError("sweep span is smaller than one bin")
        if self.mode == "search" and self.bin_hz > NARROWBAND_HZ:
            raise ValueError(
                f"search bin {self.bin_hz} Hz is wider than the narrowband "
                f"threshold {NARROWBAND_HZ:g} Hz. Use SweepPlan.overview for a "
                "coarse span."
            )

    @classmethod
    def search(
        cls,
        start_hz: int = DEFAULT_START_HZ,
        stop_hz: int = DEFAULT_STOP_HZ,
        bin_hz: int = DEFAULT_BIN_HZ,
    ) -> SweepPlan:
        return cls(start_hz, stop_hz, bin_hz, "search")

    @classmethod
    def overview(
        cls,
        start_hz: int = DEFAULT_START_HZ,
        stop_hz: int = DEFAULT_STOP_HZ,
        bin_hz: int = DEFAULT_BIN_HZ,
    ) -> SweepPlan:
        return cls(start_hz, stop_hz, bin_hz, "overview")
