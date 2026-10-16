"""IQ length is part of the capture, not a correlation input."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from quietroom.radio.device import DeviceError


def require_iq(samples: np.ndarray, expected: int) -> np.ndarray:
    """Return `samples` when it is exactly `expected` long. Otherwise raise.

    An empty or short buffer is a failed capture. It is not silence.
    """
    n = len(samples)
    if n != expected or n == 0:
        raise DeviceError(f"capture has {n} samples, expected {expected}")
    return samples


@dataclass(frozen=True)
class IqCapture:
    """A buffer that already passed `require_iq`."""

    samples: np.ndarray
    expected: int

    def __post_init__(self) -> None:
        require_iq(self.samples, self.expected)
