import pytest

from quietroom.radio import hackrf
from quietroom.engine.plan import SweepPlan
from quietroom.radio.device import DeviceError

_PLAN = SweepPlan.overview(100_000_000, 200_000_000, 1_000_000)


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
    sweeps = list(dev.sweep(_PLAN, cycles=2))
    assert len(sweeps) == 2
    first = sweeps[0]
    assert first.power_dbm[0] == -90.0   # 100 MHz bin, cycle 1
    assert first.power_dbm[1] == -70.0   # 200 MHz bin, cycle 1
    assert first.freqs_hz[0] < first.freqs_hz[1]



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
        list(hackrf.HackRFDevice().sweep(_PLAN, cycles=1))


def test_sweep_missing_binary_is_a_device_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("hackrf_sweep")
    monkeypatch.setattr(hackrf.subprocess, "Popen", boom)
    with pytest.raises(DeviceError, match="hackrf_sweep"):
        list(hackrf.HackRFDevice().sweep(_PLAN))


def test_sweep_skips_malformed_lines(monkeypatch):
    lines = [
        "d, t, 100000000, 101000000, 1000000.00, 20, -90.0\n",
        "garbage line\n",
        "d, t, 100000000, 101000000, 1000000.00, 20, -89.0\n",
    ]
    monkeypatch.setattr(hackrf.subprocess, "Popen", lambda *a, **k: _FakePopen(lines))
    (s,) = hackrf.HackRFDevice().sweep(_PLAN, cycles=1)
    assert s.power_dbm[0] == -90.0


class _IQProc:
    def __init__(self, raw: bytes, code: int):
        self.stdout = self
        self._raw = raw
        self.returncode = None
        self._code = code

    def read(self, n=-1):
        return self._raw if n < 0 else self._raw[:n]

    def terminate(self):
        return None

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def kill(self):
        self.returncode = -9


def test_capture_iq_keeps_a_full_buffer_when_terminate_signals(monkeypatch):
    raw = b"\x0a\xf6" * 8
    monkeypatch.setattr(
        hackrf.subprocess, "Popen", lambda *a, **k: _IQProc(raw, code=-15),
    )
    samples = hackrf.HackRFDevice().capture_iq(100_000_000, 2_000_000, 8)
    assert len(samples) == 8


def test_capture_iq_reports_a_short_read_and_the_exit_code(monkeypatch):
    monkeypatch.setattr(
        hackrf.subprocess, "Popen", lambda *a, **k: _IQProc(b"\x00\x00" * 2, code=1),
    )
    with pytest.raises(DeviceError, match=r"2 of 8 IQ samples \(exit 1\)"):
        hackrf.HackRFDevice().capture_iq(100_000_000, 2_000_000, 8)


def test_capture_iq_missing_binary_is_a_device_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("hackrf_transfer")

    monkeypatch.setattr(hackrf.subprocess, "Popen", boom)
    with pytest.raises(DeviceError, match="hackrf_transfer"):
        hackrf.HackRFDevice().capture_iq(100_000_000, 2_000_000, 8)
