"""A device that replays canned data: powers demo mode and hardware-free tests."""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.radio.device import Device, DeviceInfo


class RecordedDevice(Device):
    """Replays a fixed list of sweeps (cycling) and a fixed IQ buffer."""

    def __init__(self, sweeps: list[PowerSpectrum], iq: np.ndarray) -> None:
        if not sweeps:
            raise ValueError("RecordedDevice needs at least one sweep")
        self._sweeps = sweeps
        self._iq = iq

    def probe(self) -> DeviceInfo:
        return DeviceInfo(serial="recorded", board="RecordedDevice", firmware="n/a")

    def sweep(
        self, f_start_hz: int, f_stop_hz: int, bin_hz: int, cycles: int = 1
    ) -> Iterator[PowerSpectrum]:
        for i in range(cycles):
            yield self._sweeps[i % len(self._sweeps)]

    def capture_iq(
        self, center_hz: int, sample_rate: int, n_samples: int
    ) -> np.ndarray:
        if len(self._iq) >= n_samples:
            return self._iq[:n_samples]
        reps = (n_samples // max(len(self._iq), 1)) + 1
        return np.tile(self._iq, reps)[:n_samples]


def _flat(power_dbm: float, timestamp: float) -> PowerSpectrum:
    freqs = np.arange(300e6, 300e6 + 1e6 * 20, 1e6)
    return PowerSpectrum(freqs, np.full(20, power_dbm, dtype=float), timestamp)


def demo_device() -> "DemoDevice":
    return DemoDevice()


class DemoDevice(RecordedDevice):
    """RecordedDevice with a clean baseline and a separate planted-bug live sweep."""

    def __init__(self) -> None:
        baseline_sweeps = [_flat(-95.0, t) for t in (1.0, 2.0, 3.0)]
        super().__init__(sweeps=baseline_sweeps, iq=np.zeros(8, dtype=np.complex64))
        live = _flat(-95.0, 10.0)
        live.power_dbm[8] = -30.0  # strong narrowband carrier at 308 MHz (covert band)
        self._live = live

    def live_sweep(self) -> Iterator[PowerSpectrum]:
        yield self._live
