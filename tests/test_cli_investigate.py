import numpy as np
import pytest

from quietroom.audio.capture import LiveBug, NoAudioMatch
from quietroom.cli import format_verdict, main
from quietroom.radio.device import DeviceError


def test_format_verdict_live_bug():
    out = format_verdict(308_000_000.0, LiveBug(0.83))
    assert "308.000" in out
    assert "0.83" in out
    assert "LIKELY LIVE BUG" in out


def test_format_verdict_clean():
    out = format_verdict(308_000_000.0, NoAudioMatch(0.05))
    assert "no audio correlation" in out.lower()
    assert "not a detectable live audio bug" in out


def test_format_verdict_rejects_a_float():
    with pytest.raises(TypeError):
        format_verdict(308_000_000.0, 0.83)  # type: ignore[arg-type]


def test_main_demo_investigate(capsys):
    # In --demo, the audio test is synthetic and returns a high correlation.
    rc = main(["--demo", "--investigate", "308000000"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "308.000" in captured.out
    assert "LIKELY LIVE BUG" in captured.out


def test_main_prints_one_line_and_exits_when_capture_fails(monkeypatch, capsys):
    class _Dead:
        def capture_iq(self, *a, **k):
            raise DeviceError("hackrf_transfer: Resource busy")

    monkeypatch.setattr("quietroom.cli.demo_device", lambda: _Dead())
    rc = main(["--demo", "--investigate", "308M"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("\n") == 1
    assert "capture failed" in out
    assert "Resource busy" in out
    assert "not a detectable live audio bug" not in out


def test_main_empty_capture_is_not_a_quiet_room(monkeypatch, capsys):
    class _Empty:
        def capture_iq(self, *a, **k):
            return np.zeros(0, dtype=np.complex64)

    monkeypatch.setattr("quietroom.cli.demo_device", lambda: _Empty())
    rc = main(["--demo", "--investigate", "308M"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "capture failed" in out
    assert "not a detectable live audio bug" not in out


def test_main_unrelated_noise_is_not_a_bug(monkeypatch, capsys):
    class _Noise:
        def capture_iq(self, center_hz, sample_rate, n_samples):
            rng = np.random.default_rng(0)
            return (rng.normal(size=n_samples) + 1j * rng.normal(size=n_samples)).astype(
                np.complex64
            )

    monkeypatch.setattr("quietroom.cli.demo_device", lambda: _Noise())
    rc = main(["--demo", "--investigate", "308M"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no audio correlation" in out.lower()
    assert "not a detectable live audio bug" in out
