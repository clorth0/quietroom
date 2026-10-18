"""Orchestrate a device + the engine into a sweep-now scan."""
from __future__ import annotations

import itertools
from collections.abc import Iterator

from quietroom.bands import BANDS, Band
from quietroom.audio.capture import CaptureFailed, audio_correlation_test
from quietroom.engine.baseline import build_baseline
from quietroom.engine.persistence import confirm_findings
from quietroom.engine.pipeline import sweep_findings
from quietroom.engine.plan import SweepPlan
from quietroom.engine.score import assemble_finding
from quietroom.engine.spectrum import Baseline, Candidate, Finding, PowerSpectrum
from quietroom.radio.device import Device, DeviceError


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
    plan: SweepPlan,
    live: Iterator[PowerSpectrum] | None = None,
    bands: list[Band] = BANDS,
    k: float = 4.0,
) -> list[Finding]:
    """Take one live sweep and return ranked findings against the baseline.

    If `live` is given (e.g. the demo device's planted-bug sweep) it is used;
    otherwise a fresh sweep is pulled from the device.
    """
    if live is not None:
        spectrum = next(iter(live))
    else:
        spectrum = next(iter(device.sweep(
            plan.start_hz, plan.stop_hz, plan.bin_hz, cycles=1,
        )))
    return sweep_findings(spectrum, baseline, plan, k=k, bands=bands)


def confirmed_findings(
    device: Device,
    baseline: Baseline,
    n_sweeps: int,
    min_hits: int,
    plan: SweepPlan,
    live: Iterator[PowerSpectrum] | None = None,
    bands: list[Band] = BANDS,
    k: float = 4.0,
) -> list[Finding]:
    """Take `n_sweeps` live sweeps; keep emitters seen in at least `min_hits`."""
    if live is not None:
        spectra = list(itertools.islice(live, n_sweeps))
    else:
        spectra = list(device.sweep(
            plan.start_hz, plan.stop_hz, plan.bin_hz, cycles=n_sweeps,
        ))
    if len(spectra) < n_sweeps:
        raise DeviceError(f"device yielded {len(spectra)} of {n_sweeps} sweeps")
    batches = [sweep_findings(s, baseline, plan, k=k, bands=bands) for s in spectra]
    return confirm_findings(batches, min_hits=min_hits)


def investigate(
    device: Device,
    candidate: Candidate,
    *,
    bands: list[Band] = BANDS,
    **audio_kwargs,
) -> Finding:
    """Run the audio-correlation test on a candidate and re-score it.

    The re-score crosses the same scoring seam as a fresh sweep, with the
    audio correlation added through it — so the detector roster, band
    labelling and fusion an investigator sees are the sweep's own.
    """
    verdict = audio_correlation_test(
        device, int(candidate.center_freq_hz), **audio_kwargs,
    )
    if isinstance(verdict, CaptureFailed):
        raise DeviceError(verdict.reason)
    return assemble_finding(candidate, bands, audio_correlation=verdict.correlation)
