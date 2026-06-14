import numpy as np

from quietroom.audio.signal import make_chirp, resample_to


def test_chirp_length_and_range():
    sig = make_chirp(0.5, 8000)
    assert len(sig) == 4000
    assert sig.dtype == np.float32
    assert np.max(np.abs(sig)) <= 1.0 + 1e-6
    assert np.any(sig != 0)


def test_resample_changes_length_preserving_shape():
    x = make_chirp(1.0, 8000)        # 8000 samples
    y = resample_to(x, 2000)         # downsample to 2000
    assert len(y) == 2000


def test_resample_zero_or_negative_returns_empty():
    assert len(resample_to(np.ones(10), 0)) == 0
