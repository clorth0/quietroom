"""Orchestrate a device + the engine into a sweep-now scan."""
from __future__ import annotations

from collections.abc import Iterator

from quietroom.engine.baseline import build_baseline
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum
from quietroom.radio.device import Device


def capture_baseline(
    device: Device,
    f_start_hz: int,
    f_stop_hz: int,
    bin_hz: int,
    cycles: int = 5,
) -> Baseline:
    """Collect `cycles` sweeps from the device and average them into a baseline."""
    spectra = list(device.sweep(f_start_hz, f_stop_hz, bin_hz, cycles=cycles))
    return build_baseline(spectra)


def live_findings(
    device: Device,
    baseline: Baseline,
    live: Iterator[PowerSpectrum] | None = None,
    f_start_hz: int = 0,
    f_stop_hz: int = 0,
    bin_hz: int = 0,
) -> list[Finding]:
    """Take one live sweep and return ranked findings against the baseline.

    If `live` is given (e.g. the demo device's planted-bug sweep) it is used;
    otherwise a fresh sweep is pulled from the device.
    """
    if live is not None:
        spectrum = next(iter(live))
    else:
        spectrum = next(iter(device.sweep(f_start_hz, f_stop_hz, bin_hz, cycles=1)))
    return sweep_findings(spectrum, baseline)
