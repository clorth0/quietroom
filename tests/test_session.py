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


def test_spectrum_payload_has_exact_named_shape():
    import numpy as np

    from quietroom.engine.spectrum import PowerSpectrum
    from quietroom.web.session import SpectrumPayload, spectrum_payload

    payload = spectrum_payload(
        PowerSpectrum(
            freqs_hz=np.array([300_000_000.0, 301_000_000.0]),
            power_dbm=np.array([-100.0, -90.5]),
            timestamp=0.0,
        )
    )
    assert set(SpectrumPayload.__annotations__) == {"f0", "f1", "powers"}
    assert payload == {"f0": 300_000_000.0, "f1": 301_000_000.0, "powers": [-100.0, -90.5]}
    assert list(payload) == ["f0", "f1", "powers"]
    assert type(payload["f0"]) is float and type(payload["f1"]) is float
    assert all(type(p) is float for p in payload["powers"])


def test_finding_payload_has_exact_named_shape():
    from quietroom.engine.spectrum import Candidate, Finding
    from quietroom.web.session import FindingPayload, finding_payload

    payload = finding_payload(
        Finding(
            candidate=Candidate(
                center_freq_hz=308_000_000.0, bandwidth_hz=25_000.0,
                peak_power_dbm=-40.0, snr_over_baseline_db=30.0,
            ),
            score=87.26, band_label="ISM", raw_score=123.44, reasons=["loud"],
        )
    )
    assert set(FindingPayload.__annotations__) == {
        "freq_hz", "freq_mhz", "score", "raw_score", "band", "reasons",
    }
    assert payload == {
        "freq_hz": 308_000_000.0, "freq_mhz": 308.0, "score": 87.3,
        "raw_score": 123.4, "band": "ISM", "reasons": ["loud"],
    }
    assert list(payload) == ["freq_hz", "freq_mhz", "score", "raw_score", "band", "reasons"]
    assert type(payload["freq_hz"]) is float and type(payload["score"]) is float
    assert type(payload["band"]) is str and all(type(r) is str for r in payload["reasons"])


def test_sweep_payloads_keep_exact_json_wire_format():
    import json

    s = ScanSession(streaming_demo_device(n_clean=3),
                    f_start_hz=300_000_000, f_stop_hz=320_000_000,
                    bin_hz=1_000_000, overview=True)
    s.capture_baseline(cycles=3)
    spectrum, findings = s.sweep_once()
    # Byte-identical wire format: TypedDict is a plain dict at runtime.
    assert json.dumps(spectrum, sort_keys=True).startswith('{"f0": ')
    assert set(spectrum) == {"f0", "f1", "powers"}
    assert findings and set(findings[0]) == {
        "freq_hz", "freq_mhz", "score", "raw_score", "band", "reasons",
    }
    assert json.loads(json.dumps(findings[0])) == findings[0]
