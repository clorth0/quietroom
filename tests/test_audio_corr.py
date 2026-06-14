import numpy as np

from quietroom.engine.detectors.audio_corr import (
    envelope_correlation, score_audio,
)


def test_correlated_envelope_scores_high():
    t = np.linspace(0, 1, 500)
    audio = np.sin(2 * np.pi * 5 * t)
    rf_envelope = 3.0 * audio + 0.01 * np.random.default_rng(0).normal(size=t.size)
    corr = envelope_correlation(rf_envelope, audio)
    assert corr > 0.9


def test_uncorrelated_envelope_scores_low():
    rng = np.random.default_rng(1)
    audio = rng.normal(size=500)
    rf_envelope = rng.normal(size=500)
    corr = envelope_correlation(rf_envelope, audio)
    assert corr < 0.3


def test_score_audio_fires_above_threshold():
    r = score_audio(0.82)
    assert r.name == "audio"
    assert r.contribution > 0
    assert "0.82" in r.reasons[0]


def test_score_audio_silent_below_threshold():
    r = score_audio(0.1)
    assert r.contribution == 0.0
    assert r.reasons == []


def test_empty_inputs_return_zero():
    assert envelope_correlation(np.array([]), np.array([])) == 0.0
