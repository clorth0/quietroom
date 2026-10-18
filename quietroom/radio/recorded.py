"""A device that replays canned data: powers demo mode and hardware-free tests."""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from quietroom.engine.plan import SweepPlan
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
        self, plan: SweepPlan, cycles: int = 1
    ) -> Iterator[PowerSpectrum]:
        # Replays fixed sweeps: the plan selects nothing here, by design.
        for i in range(cycles):
            yield self._sweeps[i % len(self._sweeps)]

    def capture_iq(
        self, center_hz: int, sample_rate: int, n_samples: int
    ) -> np.ndarray:
        if len(self._iq) >= n_samples:
            return self._iq[:n_samples]
        reps = (n_samples // max(len(self._iq), 1)) + 1
        return np.tile(self._iq, reps)[:n_samples]


DEMO_BUG_HZ = 308_000_000


def synthetic_bug_iq(
    center_hz: int, sample_rate: int, n_samples: int, bug_hz: int = DEMO_BUG_HZ
) -> np.ndarray:
    """IQ for a demo room: an AM bug at bug_hz carrying the test chirp, plus a
    DC/LO-leakage spike, as seen by a radio tuned to center_hz."""
    from quietroom.audio.signal import make_chirp, resample_to

    duration = n_samples / sample_rate
    mod = resample_to(make_chirp(duration, 48_000), n_samples)
    t = np.arange(n_samples) / sample_rate
    bug = (1.0 + 0.5 * mod) * np.exp(1j * 2 * np.pi * (bug_hz - center_hz) * t)
    rng = np.random.default_rng(0)
    spike = 2.0 + 0.2 * rng.normal(size=n_samples)
    return (bug + spike).astype(np.complex64)


DEMO_FLOOR_DBM = -95.0
DEMO_BUG_DBM = -30.0


def spectrum_grid(
    start_hz: int,
    stop_hz: int,
    bin_hz: int,
    power_dbm: float,
    timestamp: float,
    bug_hz: int | None = None,
) -> PowerSpectrum:
    """A uniform grid from start to stop at bin_hz. One bin can hold the demo bug."""
    if bin_hz <= 0 or stop_hz <= start_hz:
        raise ValueError("need a positive bin and a stop above the start")
    freqs = np.arange(float(start_hz), float(stop_hz), float(bin_hz))
    if len(freqs) < 2:
        raise ValueError("sweep span must cover at least two bins")
    powers = np.full(len(freqs), power_dbm, dtype=float)
    if bug_hz is not None:
        idx = int(round((float(bug_hz) - float(start_hz)) / float(bin_hz)))
        if not 0 <= idx < len(powers):
            raise ValueError(f"bug at {bug_hz} Hz is outside {start_hz}-{stop_hz} Hz")
        powers[idx] = DEMO_BUG_DBM
    return PowerSpectrum(freqs, powers, timestamp)


def demo_device() -> DemoDevice:
    return DemoDevice()


class DemoDevice(Device):
    """Clean baseline sweeps plus a separate planted-bug live sweep, both on the requested grid."""

    def probe(self) -> DeviceInfo:
        return DeviceInfo(serial="demo", board="DemoDevice", firmware="n/a")

    def sweep(
        self, plan: SweepPlan, cycles: int = 1
    ) -> Iterator[PowerSpectrum]:
        for i in range(cycles):
            yield spectrum_grid(
                plan.start_hz, plan.stop_hz, plan.bin_hz,
                DEMO_FLOOR_DBM, float(i + 1),
            )

    def live_sweep(self, plan: SweepPlan) -> Iterator[PowerSpectrum]:
        """Endless planted-bug sweeps on the same grid as `sweep`."""
        while True:
            yield spectrum_grid(
                plan.start_hz, plan.stop_hz, plan.bin_hz,
                DEMO_FLOOR_DBM, 10.0, bug_hz=DEMO_BUG_HZ,
            )

    def capture_iq(self, center_hz, sample_rate, n_samples):
        return synthetic_bug_iq(center_hz, sample_rate, n_samples)


class StreamingDemoDevice(Device):
    """Serves `n_clean` clean sweeps, then planted-bug sweeps, on the requested grid."""

    def __init__(self, n_clean: int = 3) -> None:
        self._n_clean = n_clean
        self._served = 0

    def probe(self) -> DeviceInfo:
        return DeviceInfo(serial="demo", board="StreamingDemoDevice", firmware="n/a")

    def sweep(self, plan, cycles=1):
        for _ in range(cycles):
            clean = self._served < self._n_clean
            self._served += 1
            bug = None if clean else DEMO_BUG_HZ
            yield spectrum_grid(
                plan.start_hz, plan.stop_hz, plan.bin_hz, DEMO_FLOOR_DBM,
                float(self._served), bug_hz=bug,
            )

    def capture_iq(self, center_hz, sample_rate, n_samples):
        return synthetic_bug_iq(center_hz, sample_rate, n_samples)


def streaming_demo_device(n_clean: int = 3) -> StreamingDemoDevice:
    return StreamingDemoDevice(n_clean=n_clean)
