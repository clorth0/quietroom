from quietroom.engine.detectors.audio_corr import score_audio


def test_score_audio_fires_above_threshold():
    r = score_audio(0.82)
    assert r.name == "audio"
    assert r.contribution > 0
    assert "0.82" in r.reasons[0]


def test_score_audio_silent_below_threshold():
    r = score_audio(0.1)
    assert r.contribution == 0.0
    assert r.reasons == []
