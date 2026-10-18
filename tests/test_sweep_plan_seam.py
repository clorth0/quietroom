"""SweepPlan crosses the scan-Device seam as one typed value.

Characterization: device-visible behaviour (grid start/spacing/order,
cycle count, clean-then-bug ordering, session defaults) is pinned through
the recorded/demo adapters -- the hardware-free test adapters at the seam.
A non-default plan is used throughout so dropping or overriding any one
plan field at the seam (start, stop, or bin) reddens a test below.
"""
import numpy as np

from quietroom.engine.plan import SweepPlan
from quietroom.radio.recorded import RecordedDevice, demo_device, streaming_demo_device
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.scan import capture_baseline
from quietroom.web.session import ScanSession

# Deliberately not the default 300-320 MHz / 25 kHz plan.
PLAN = SweepPlan.overview(305_000_000, 315_000_000, 100_000)


def test_demo_sweep_honours_every_plan_field():
    spec = next(iter(demo_device().sweep(PLAN, cycles=1)))
    assert spec.freqs_hz[0] == PLAN.start_hz
    assert spec.freqs_hz[-1] == PLAN.stop_hz - PLAN.bin_hz
    assert len(spec.freqs_hz) == (PLAN.stop_hz - PLAN.start_hz) // PLAN.bin_hz
    assert np.all(np.diff(spec.freqs_hz) == PLAN.bin_hz)  # stepped in order


def test_demo_live_sweep_uses_the_same_plan_grid():
    live = next(iter(demo_device().live_sweep(PLAN)))
    assert live.freqs_hz[0] == PLAN.start_hz
    assert np.all(np.diff(live.freqs_hz) == PLAN.bin_hz)
    assert float(np.max(live.power_dbm)) > -40.0  # planted bug on that grid


def test_streaming_demo_order_is_clean_then_bug_across_the_seam():
    dev = streaming_demo_device(n_clean=2)
    sweeps = list(dev.sweep(PLAN, cycles=3))
    assert [float(np.max(s.power_dbm)) for s in sweeps[:2]] == [-95.0, -95.0]
    assert float(np.max(sweeps[2].power_dbm)) == -30.0
    assert sweeps[0].freqs_hz[0] == PLAN.start_hz


def test_recorded_device_cycles_regardless_of_plan_fields():
    a = PowerSpectrum(np.array([1.0]), np.array([-95.0]), 1.0)
    b = PowerSpectrum(np.array([1.0]), np.array([-90.0]), 2.0)
    dev = RecordedDevice(sweeps=[a, b], iq=np.zeros(4, dtype=np.complex64))
    out = list(dev.sweep(PLAN, cycles=3))
    assert [s.power_dbm[0] for s in out] == [-95.0, -90.0, -95.0]


def test_capture_baseline_takes_the_plan_not_raw_ints():
    baseline = capture_baseline(demo_device(), PLAN, cycles=3)
    assert baseline.sweep_count == 3
    assert baseline.freqs_hz[0] == PLAN.start_hz
    assert baseline.freqs_hz[1] - baseline.freqs_hz[0] == PLAN.bin_hz


def test_session_holds_the_plan_not_copies_of_its_fields():
    s = ScanSession(streaming_demo_device())
    assert s.plan == SweepPlan.search()
    assert not hasattr(s, "f_start_hz")
    assert not hasattr(s, "f_stop_hz")
    assert not hasattr(s, "bin_hz")


def test_session_sweep_once_crosses_the_seam_with_its_plan():
    s = ScanSession(
        streaming_demo_device(n_clean=3),
        f_start_hz=305_000_000, f_stop_hz=315_000_000,
        bin_hz=100_000, overview=True,
    )
    assert s.plan == PLAN
    s.capture_baseline(cycles=3)
    spectrum, _ = s.sweep_once()
    assert spectrum["f0"] == PLAN.start_hz
    assert len(spectrum["powers"]) == 100
