from quietroom.radio.recorded import streaming_demo_device
from quietroom.web.session import ScanSession


def test_baseline_then_sweep_finds_bug():
    s = ScanSession(streaming_demo_device(n_clean=3),
                    f_start_hz=300_000_000, f_stop_hz=320_000_000,
                    bin_hz=1_000_000, overview=True)
    assert s.capture_baseline(cycles=3) == 3
    spectrum, findings = s.sweep_once()
    assert "powers" in spectrum and spectrum["f0"] < spectrum["f1"]
    assert findings and findings[0]["score"] > 50.0
    assert findings[0]["freq_mhz"] == 308.0


def test_sweep_without_baseline_returns_empty_findings():
    s = ScanSession(streaming_demo_device())
    spectrum, findings = s.sweep_once()
    assert findings == []


def test_investigate_rescores_the_suspect_from_the_capture():
    import numpy as np

    s = ScanSession(streaming_demo_device(n_clean=3),
                    f_start_hz=300_000_000, f_stop_hz=320_000_000,
                    bin_hz=1_000_000, overview=True)
    s.capture_baseline(cycles=3)
    s.sweep_once()
    payload = s.investigate(
        308_000_000.0,
        play_record=lambda sig, fs: np.asarray(sig, float),
        duration_s=0.25,
    )
    assert payload["score"] >= 90.0
    assert any("correlates with room audio" in r for r in payload["reasons"])


def test_investigate_unknown_frequency_raises():
    s = ScanSession(streaming_demo_device())
    s.capture_baseline(cycles=3)
    s.sweep_once()
    try:
        s.investigate(999_000_000.0)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


def test_repeated_sweeps_confirm_the_bug():
    s = ScanSession(streaming_demo_device(n_clean=3),
                    f_start_hz=300_000_000, f_stop_hz=320_000_000,
                    bin_hz=1_000_000, overview=True)
    s.capture_baseline(cycles=3)
    _, first = s.sweep_once()
    _, second = s.sweep_once()
    assert any("unconfirmed" in r for r in first[0]["reasons"])
    assert any("persistent" in r for r in second[0]["reasons"])
    assert second[0]["score"] >= first[0]["score"]


def test_new_baseline_resets_persistence():
    s = ScanSession(streaming_demo_device(n_clean=3))
    s.capture_baseline(cycles=3)
    s.sweep_once()
    s.sweep_once()
    assert s.tracker.sweeps_seen == 2
    s.capture_baseline(cycles=1)
    assert s.tracker.sweeps_seen == 0
