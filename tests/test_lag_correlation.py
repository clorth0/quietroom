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
