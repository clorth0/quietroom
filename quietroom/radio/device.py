"""Abstract radio device interface for the detection engine."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from quietroom.engine.spectrum import PowerSpectrum


class DeviceError(RuntimeError):
    """Raised when a radio device cannot perform a requested operation."""


@dataclass
class DeviceInfo:
    serial: str
    board: str
    firmware: str


class Device(ABC):
    """A receive-only SDR: wideband sweep plus tuned-IQ capture."""

    @abstractmethod
    def probe(self) -> DeviceInfo | None:
        """Return device info, or None if no device is present."""

    @abstractmethod
    def sweep(
        self, f_start_hz: int, f_stop_hz: int, bin_hz: int, cycles: int = 1
    ) -> Iterator[PowerSpectrum]:
        """Yield up to `cycles` full power-vs-frequency sweeps."""

    @abstractmethod
    def capture_iq(
        self, center_hz: int, sample_rate: int, n_samples: int
    ) -> np.ndarray:
        """Capture `n_samples` complex64 IQ samples at `center_hz`."""
