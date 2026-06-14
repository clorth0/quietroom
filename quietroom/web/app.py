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

    return app, socketio
