"""Flask + Socket.IO transport for Quietroom (localhost-only)."""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from flask import Flask, abort, render_template, request
from flask_socketio import SocketIO

from quietroom.radio.recorded import streaming_demo_device
from quietroom.web.session import ScanSession

_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"


def emit_tick(session, emit_fn) -> None:
    """Run one sweep and emit its spectrum + findings via emit_fn(name, payload)."""
    spectrum, findings = session.sweep_once()
    emit_fn("sweep", spectrum)
    emit_fn("findings", {"findings": findings})


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_COOKIE = "quietroom_token"


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(
    device=None,
    demo: bool = False,
    token: str | None = None,
    extra_hosts: frozenset[str] = frozenset(),
) -> tuple[Flask, SocketIO]:
    allowed_hosts = LOOPBACK_HOSTS | set(extra_hosts)

    def _origin_ok(origin, environ=None) -> bool:
        # Only pages served by this UI may open the socket. (Passing [] here,
        # as before, disables engineio's origin check entirely: any website
        # could drive the sweep and the speaker.)
        return (urlsplit(origin).hostname or "") in allowed_hosts

    app = Flask(__name__)
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=_origin_ok)

    def _token_ok() -> bool:
        if token is None:
            return True
        given = request.args.get("token") or request.cookies.get(TOKEN_COOKIE) or ""
        return hmac.compare_digest(given.encode(), token.encode())

    @app.before_request
    def _guard():
        # Host check defeats DNS rebinding (evil.example resolving to 127.0.0.1).
        if (request.host.rsplit(":", 1)[0].strip("[]")) not in allowed_hosts:
            abort(403)
        if not _token_ok():
            abort(403)

    if device is None:
        if demo:
            device = streaming_demo_device()
        else:
            from quietroom.radio.hackrf import HackRFDevice

            device = HackRFDevice()
    session = ScanSession(device) if device is not None else None
    app.config["DEMO"] = demo
    app.config["RUNNING"] = False
    app.scan_session = session            # exposed for tests and handlers

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = _CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        if token is not None and request.args.get("token"):
            resp.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="Strict")
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

    @socketio.on("connect")
    def _on_connect(auth=None):
        if token is None:
            return True
        given = (auth or {}).get("token") if isinstance(auth, dict) else None
        if given and hmac.compare_digest(str(given).encode(), token.encode()):
            return True
        return _token_ok()

    @socketio.on("capture_baseline")
    def _on_capture_baseline(data):
        if session is None:
            emit("error", {"message": "no device"})
            return
        try:
            cycles = int((data or {}).get("cycles", 5))
        except (TypeError, ValueError):
            emit("error", {"message": "invalid cycles"})
            return
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

    def _sweep_loop():
        while app.config["RUNNING"]:
            emit_tick(session, socketio.emit)
            socketio.sleep(0.5)

    @socketio.on("start")
    def _on_start(data):
        if session is None or app.config["RUNNING"]:
            return
        app.config["RUNNING"] = True
        socketio.start_background_task(_sweep_loop)

    @socketio.on("stop")
    def _on_stop(data):
        app.config["RUNNING"] = False

    return app, socketio


def build_server(demo: bool = False, host: str = "127.0.0.1", port: int = 8770,
                 token: str | None = None):
    extra = frozenset() if is_loopback(host) else frozenset({host})
    app, socketio = create_app(demo=demo, token=token, extra_hosts=extra)
    app.config["HOST"] = host
    app.config["PORT"] = port
    return app, socketio


def _run(socketio, app, host, port):  # pragma: no cover - blocking server
    # Werkzeug dev server: acceptable for a single-user localhost tool.
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="quietroom-web",
                                     description="Quietroom localhost web UI.")
    parser.add_argument("--demo", action="store_true", help="run with no hardware")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind host (loopback unless --allow-remote)")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit a non-loopback --host; requires QUIETROOM_TOKEN")
    args = parser.parse_args(argv)
    token = os.environ.get("QUIETROOM_TOKEN") or None
    if not is_loopback(args.host):
        if not args.allow_remote:
            parser.error(f"refusing to bind {args.host}: pass --allow-remote "
                         "(prefer an SSH tunnel or Tailscale to 127.0.0.1)")
        if not token or len(token) < 16:
            parser.error("--allow-remote needs QUIETROOM_TOKEN set (16+ chars)")
    app, socketio = build_server(demo=args.demo, host=args.host, port=args.port,
                                 token=token)
    url = f"http://{args.host}:{args.port}"
    print(f"Quietroom web UI on {url}{'/?token=<QUIETROOM_TOKEN>' if token else ''}"
          f"  (demo={args.demo})")
    _run(socketio, app, args.host, args.port)
    return 0
