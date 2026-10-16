from quietroom.radio.device import Device, DeviceError, DeviceInfo
from quietroom.web.app import create_app


class _BrokenRadio(Device):
    def probe(self):
        return DeviceInfo("x", "x", "x")

    def sweep(self, *a, **k):
        raise DeviceError("hackrf_sweep exited (code 1): hackrf_open() failed: Resource busy")
        yield  # pragma: no cover

    def capture_iq(self, *a, **k):
        raise DeviceError("hackrf_transfer: Resource busy")


def _errors(client):
    return [e["args"][0]["message"] for e in client.get_received() if e["name"] == "error"]


def test_capture_baseline_device_error_reaches_the_ui():
    app, socketio = create_app(device=_BrokenRadio())
    client = socketio.test_client(app)
    client.emit("capture_baseline", {"cycles": 3})
    msgs = _errors(client)
    assert msgs and "baseline failed" in msgs[0] and "Resource busy" in msgs[0]


def test_unexpected_baseline_failure_also_reported():
    class Weird(_BrokenRadio):
        def sweep(self, *a, **k):
            raise ValueError("spectra do not share enough frequency bins to compare")
            yield  # pragma: no cover

    app, socketio = create_app(device=Weird())
    client = socketio.test_client(app)
    client.emit("capture_baseline", {"cycles": 2})
    assert any("frequency bins" in m for m in _errors(client))


def test_frontend_marks_errors_visibly():
    app, _ = create_app(demo=True)
    js = app.test_client().get("/static/app.js").get_data(as_text=True)
    css = app.test_client().get("/static/style.css").get_data(as_text=True)
    assert "status-error" in js and ".status-error" in css


def test_iq_capture_failure_is_not_reported_as_clean():
    # A failed capture used to leave zeros -> correlation 0 -> "not a bug".
    import numpy as np

    from quietroom.audio.capture import CaptureFailed, audio_correlation_test
    from quietroom.cli import format_verdict

    verdict = audio_correlation_test(
        _BrokenRadio(), 308_000_000, duration_s=0.05,
        play_record=lambda s, fs: np.asarray(s, float),
    )
    assert isinstance(verdict, CaptureFailed)
    text = format_verdict(308_000_000.0, verdict)
    assert "capture failed" in text
    assert "Resource busy" in text
    assert "not a detectable live audio bug" not in text


def test_investigate_device_error_reaches_the_ui():
    from quietroom.radio.recorded import StreamingDemoDevice

    class FlakyIQ(StreamingDemoDevice):
        def capture_iq(self, *a, **k):
            raise DeviceError("hackrf_transfer: Resource busy")

    app, socketio = create_app(device=FlakyIQ())
    app.config["DEMO"] = True            # loopback mic; the radio still fails
    app.scan_session.capture_baseline(cycles=3)
    _, findings = app.scan_session.sweep_once()
    client = socketio.test_client(app)
    client.get_received()
    client.emit("investigate", {"freq_hz": findings[0]["freq_hz"]})
    msgs = _errors(client)
    assert msgs and "investigate failed" in msgs[0] and "Resource busy" in msgs[0]
