from quietroom.engine.spectrum import Candidate
from quietroom.radio.recorded import demo_device
from quietroom.scan import investigate


def _candidate():
    # 308 MHz, narrowband, strong: catalog(unknown)+signatures already make it suspicious.
    return Candidate(center_freq_hz=308_000_000.0, bandwidth_hz=1_000_000.0,
                     peak_power_dbm=-30.0, snr_over_baseline_db=65.0)


def test_investigate_high_correlation_raises_score_and_adds_reason():
    dev = demo_device()

    def fake_audio_test(device, center_hz, **kw):
        return 0.9   # strong envelope correlation -> live bug

    finding = investigate(dev, _candidate(), audio_test=fake_audio_test)
    assert any("correlates with room audio" in r for r in finding.reasons)
    assert finding.score >= 100.0   # audio adds 50 on top of an already-high score
    assert "audio" in finding.breakdown


def test_investigate_low_correlation_no_audio_reason():
    dev = demo_device()

    def fake_audio_test(device, center_hz, **kw):
        return 0.1

    finding = investigate(dev, _candidate(), audio_test=fake_audio_test)
    assert not any("correlates with room audio" in r for r in finding.reasons)
    assert finding.breakdown["audio"] == 0.0
