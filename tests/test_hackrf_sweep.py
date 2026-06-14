import numpy as np

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
