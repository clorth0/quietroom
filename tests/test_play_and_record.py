import sys
import types

import numpy as np

from quietroom.audio import capture


def test_play_and_record_uses_sounddevice_playrec(monkeypatch):
    calls = {}

    fake_sd = types.SimpleNamespace()

    def fake_playrec(data, samplerate, channels):
        calls["samplerate"] = samplerate
        calls["channels"] = channels
        calls["n"] = len(data)
        return np.ones((len(data), 1), dtype="float32")

    fake_sd.playrec = fake_playrec
    fake_sd.wait = lambda: calls.setdefault("waited", True)

    # capture.py imports sounddevice lazily as `import sounddevice as sd`;
    # inject a fake module so no PortAudio is needed.
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    sig = np.zeros(800, dtype="float32")
    rec = capture.play_and_record(sig, 8000)

    assert calls["samplerate"] == 8000
    assert calls["channels"] == 1
    assert calls["waited"] is True
    assert rec.shape == (800,)        # flattened to mono 1-D
