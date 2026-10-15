
from quietroom.radio import hackrf


class _FakePopen:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_sweep_yields_power_spectrum(monkeypatch):
    # Two cycles of a two-segment sweep (100 MHz and 200 MHz bins).
    lines = [
        "d, t, 100000000, 101000000, 1000000.00, 20, -90.0\n",
        "d, t, 200000000, 201000000, 1000000.00, 20, -70.0\n",
        "d, t, 100000000, 101000000, 1000000.00, 20, -89.0\n",  # repeat -> flush cycle 1
        "d, t, 200000000, 201000000, 1000000.00, 20, -71.0\n",
        "d, t, 100000000, 101000000, 1000000.00, 20, -88.0\n",  # repeat -> flush cycle 2
    ]
    monkeypatch.setattr(hackrf.subprocess, "Popen", lambda *a, **k: _FakePopen(lines))

    dev = hackrf.HackRFDevice()
    sweeps = list(dev.sweep(100_000_000, 200_000_000, 1_000_000, cycles=2))
    assert len(sweeps) == 2
    first = sweeps[0]
    assert first.power_dbm[0] == -90.0   # 100 MHz bin, cycle 1
    assert first.power_dbm[1] == -70.0   # 200 MHz bin, cycle 1
    assert first.freqs_hz[0] < first.freqs_hz[1]


import pytest

from quietroom.radio.device import DeviceError


class _DyingPopen(_FakePopen):
    def __init__(self, lines, stderr_text, stderr):
        super().__init__(lines)
        self.returncode = 1
        if stderr is not None and hasattr(stderr, "write"):
            stderr.write(stderr_text.encode())

    def poll(self):
        return 1


def test_sweep_reports_hackrf_error_when_process_dies(monkeypatch):
    lines = ["d, t, 100000000, 101000000, 1000000.00, 20, -90.0\n"]
    monkeypatch.setattr(
        hackrf.subprocess, "Popen",
        lambda *a, **k: _DyingPopen(lines, "hackrf_open() failed: Resource busy\n",
                                    k.get("stderr")),
    )
    with pytest.raises(DeviceError, match="Resource busy"):
        list(hackrf.HackRFDevice().sweep(100_000_000, 200_000_000, 1_000_000, cycles=1))


def test_sweep_missing_binary_is_a_device_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("hackrf_sweep")
    monkeypatch.setattr(hackrf.subprocess, "Popen", boom)
    with pytest.raises(DeviceError, match="hackrf_sweep"):
        list(hackrf.HackRFDevice().sweep(100_000_000, 200_000_000, 1_000_000))


def test_sweep_skips_malformed_lines(monkeypatch):
    lines = [
        "d, t, 100000000, 101000000, 1000000.00, 20, -90.0\n",
        "garbage line\n",
        "d, t, 100000000, 101000000, 1000000.00, 20, -89.0\n",
    ]
    monkeypatch.setattr(hackrf.subprocess, "Popen", lambda *a, **k: _FakePopen(lines))
    (s,) = hackrf.HackRFDevice().sweep(100_000_000, 200_000_000, 1_000_000, cycles=1)
    assert s.power_dbm[0] == -90.0
