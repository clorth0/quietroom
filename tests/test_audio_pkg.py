def test_audio_signal_module_imports_without_sounddevice():
    # signal.py must be pure DSP: importable with no PortAudio/sounddevice present.
    import quietroom.audio.signal as s
    assert hasattr(s, "make_chirp")
