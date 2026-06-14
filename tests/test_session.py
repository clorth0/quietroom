from quietroom.radio.recorded import streaming_demo_device
from quietroom.web.session import ScanSession


def test_baseline_then_sweep_finds_bug():
    s = ScanSession(streaming_demo_device(n_clean=3),
                    f_start_hz=300_000_000, f_stop_hz=320_000_000, bin_hz=1_000_000)
    assert s.capture_baseline(cycles=3) == 3
    spectrum, findings = s.sweep_once()
    assert "powers" in spectrum and spectrum["f0"] < spectrum["f1"]
    assert findings and findings[0]["score"] > 50.0
    assert findings[0]["freq_mhz"] == 308.0


def test_sweep_without_baseline_returns_empty_findings():
    s = ScanSession(streaming_demo_device())
    spectrum, findings = s.sweep_once()
    assert findings == []


def test_investigate_uses_last_findings_and_audio_test():
    s = ScanSession(streaming_demo_device(n_clean=3),
                    f_start_hz=300_000_000, f_stop_hz=320_000_000, bin_hz=1_000_000)
    s.capture_baseline(cycles=3)
    s.sweep_once()
    payload = s.investigate(308_000_000.0, audio_test=lambda *a, **k: 0.9)
    assert payload["score"] >= 100.0
    assert any("correlates with room audio" in r for r in payload["reasons"])


def test_investigate_unknown_frequency_raises():
    s = ScanSession(streaming_demo_device())
    s.capture_baseline(cycles=3)
    s.sweep_once()
    try:
        s.investigate(999_000_000.0, audio_test=lambda *a, **k: 0.9)
        assert False, "expected KeyError"
    except KeyError:
        pass
