import pytest
import numpy as np

from quietroom.audio.signal import best_lag_correlation, make_chirp


def test_correlated_with_lag_scores_high():
    a = make_chirp(1.0, 8000).astype(float)
    lag = 120
    b = np.zeros_like(a)
    b[lag:] = a[:-lag]                           # same signal delayed by `lag`
    corr = best_lag_correlation(a, b, max_lag=300)
    assert corr > 0.9


def test_lag_outside_window_scores_lower():
    a = make_chirp(1.0, 8000).astype(float)
    lag = 500
    b = np.zeros_like(a)
    b[lag:] = a[:-lag]
    # max_lag too small to find the true alignment
    corr = best_lag_correlation(a, b, max_lag=50)
    assert corr < 0.9


def test_uncorrelated_scores_low():
    rng = np.random.default_rng(0)
    a = rng.normal(size=8000)
    b = rng.normal(size=8000)
    assert best_lag_correlation(a, b, max_lag=300) < 0.3


def test_empty_returns_zero():
    assert best_lag_correlation(np.array([]), np.array([]), max_lag=10) == 0.0


def test_aligned_sine_scores_high_at_zero_lag():
    # Migrated from the former zero-lag correlation cases: an aligned
    # scaled copy must score high even with no lag window to search.
    t = np.linspace(0, 1, 500)
    audio = np.sin(2 * np.pi * 5 * t)
    rf_envelope = 3.0 * audio + 0.01 * np.random.default_rng(0).normal(size=t.size)
    assert best_lag_correlation(rf_envelope, audio, max_lag=0) > 0.9


def test_inverted_aligned_scores_high():
    # Correlation is a magnitude: an inverted aligned copy is still a match.
    t = np.linspace(0, 1, 500)
    audio = np.sin(2 * np.pi * 5 * t)
    assert best_lag_correlation(-audio, audio, max_lag=0) > 0.99


def test_identical_noise_scores_near_one_at_zero_lag():
    # Pinpoints zero lag: white noise decorrelates in one sample, so an
    # off-by-one in the lag window drops this from ~1 to near zero.
    noise = np.random.default_rng(2).normal(size=8000)
    assert best_lag_correlation(noise, noise, max_lag=0) > 0.99


def test_constant_inputs_return_zero():
    assert best_lag_correlation(np.ones(100), np.ones(100), max_lag=10) == 0.0


def _chirp_at(fs, delay_s, dur=1.0, f0=500.0, f1=3000.0):
    t = np.arange(int(dur * fs)) / fs - delay_s
    k = (f1 - f0) / dur
    return np.sin(2 * np.pi * (f0 * t + 0.5 * k * t * t))


@pytest.mark.parametrize("frac", [0.25, 0.4, 0.5])
def test_fractional_sample_delay_still_correlates(frac):
    # Playback-to-capture latency is arbitrary, so the RF envelope is usually
    # offset from the mic by a non-integer number of 8 kHz samples. A chirp
    # reaching 3 kHz (near Nyquist) must still score as a match.
    fs = 8_000
    a = _chirp_at(fs, 0.0)
    b = _chirp_at(fs, frac / fs)
    assert best_lag_correlation(a, b, 100) > 0.95


def test_noise_still_scores_low_with_envelope_peak():
    rng = np.random.default_rng(1)
    a = _chirp_at(8_000, 0.0)
    assert best_lag_correlation(a, rng.normal(size=len(a)), 2000) < 0.2
