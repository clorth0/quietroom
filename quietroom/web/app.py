"""Flask + Socket.IO transport for Quietroom (localhost-only)."""
from __future__ import annotations

from flask import Flask, render_template
from flask_socketio import SocketIO

from quietroom.radio.recorded import streaming_demo_device
from quietroom.web.session import ScanSession

_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"


def create_app(device=None, demo: bool = False) -> tuple[Flask, SocketIO]:
    app = Flask(__name__)
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=[])

    if device is None:
        device = streaming_demo_device() if demo else None
    session = ScanSession(device) if device is not None else None
    app.config["DEMO"] = demo
    app.scan_session = session            # exposed for tests and handlers

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = _CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.route("/")
    def index():
        return render_template("index.html")

    from flask_socketio import emit

    MIN_HZ, MAX_HZ = 1_000_000, 6_000_000_000

    def _audio_kwargs():
        # In demo mode, synthesize a positive correlation (no mic/speaker/radio).
        if app.config["DEMO"]:
            return {"audio_test": lambda *a, **k: 0.85}
        return {}

    @socketio.on("capture_baseline")
    def _on_capture_baseline(data):
        if session is None:
            emit("error", {"message": "no device"})
            return
        cycles = int((data or {}).get("cycles", 5))
        count = session.capture_baseline(cycles=max(1, min(cycles, 30)))
        emit("baseline_ready", {"sweep_count": count})

    @socketio.on("investigate")
    def _on_investigate(data):
        if session is None:
            emit("error", {"message": "no device"})
            return
        try:
            freq_hz = float((data or {}).get("freq_hz"))
        except (TypeError, ValueError):
            emit("error", {"message": "invalid frequency"})
            return
        if not (MIN_HZ <= freq_hz <= MAX_HZ):
            emit("error", {"message": "frequency out of range"})
            return
        try:
            payload = session.investigate(freq_hz, **_audio_kwargs())
        except KeyError:
            emit("error", {"message": "no current suspect at that frequency"})
            return
        emit("finding", payload)

    return app, socketio
