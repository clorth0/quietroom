"""Flask + Socket.IO transport for Quietroom (localhost-only)."""
from __future__ import annotations

import argparse
import hmac
import os

from flask import Flask, abort, render_template, request
from flask_socketio import SocketIO

from quietroom.bands import REGIONS
from quietroom.radio.recorded import streaming_demo_device
from quietroom.web.bind import Bind, LocalBind, origin_checker, resolve_bind
from quietroom.web.commands import CommandContext, dispatch
from quietroom.web.session import ScanSession

_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"


def emit_tick(session, emit_fn) -> None:
    """Run one sweep and emit its spectrum + findings via emit_fn(name, payload)."""
    spectrum, findings = session.sweep_once()
    emit_fn("sweep", spectrum)
    emit_fn("findings", {"findings": findings})


def safe_tick(session, emit_fn) -> bool:
    """emit_tick that turns a failure into an 'error' event; False means stop."""
    try:
        emit_tick(session, emit_fn)
    except Exception as e:  # noqa: BLE001 - surface any device/engine failure
        emit_fn("error", {"message": f"sweep failed: {e}"})
        return False
    return True


TOKEN_COOKIE = "quietroom_token"


def create_app(
    device=None,
    demo: bool = False,
    bind: Bind | None = None,
    region: str = "us",
) -> tuple[Flask, SocketIO]:
    if bind is None:
        bind = LocalBind()
    allowed_hosts = bind.allowed_hosts
    token = bind.token
    origin_ok = origin_checker(bind)

    app = Flask(__name__)
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=origin_ok)

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
    session = ScanSession(device, region=region) if device is not None else None
    app.config["DEMO"] = demo
    app.config["RUNNING"] = False
    app.scan_session = session  # type: ignore[attr-defined]  # for tests/handlers

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
        return render_template(
            "index.html",
            regions=sorted(REGIONS),
            region=session.region if session is not None else region,
        )

    from flask_socketio import emit

    ctx = CommandContext(app, token, socketio, emit)

    @socketio.on("connect")
    def _on_connect(auth=None):
        return dispatch("connect", auth, ctx)

    @socketio.on("set_region")
    def _on_set_region(data):
        return dispatch("set_region", data, ctx)

    @socketio.on("capture_baseline")
    def _on_capture_baseline(data):
        return dispatch("capture_baseline", data, ctx)

    @socketio.on("investigate")
    def _on_investigate(data):
        return dispatch("investigate", data, ctx)

    @socketio.on("start")
    def _on_start(data):
        return dispatch("start", data, ctx)

    @socketio.on("stop")
    def _on_stop(data):
        return dispatch("stop", data, ctx)

    return app, socketio


def build_server(demo: bool = False, host: str = "127.0.0.1", port: int = 8770,
                 token: str | None = None, region: str = "us", bind: Bind | None = None):
    if bind is None:
        bind = resolve_bind(host, allow_remote=False, token=token)
    app, socketio = create_app(demo=demo, bind=bind, region=region)
    app.config["HOST"] = bind.host
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
    parser.add_argument("--region", choices=sorted(REGIONS), default="us",
                        help="initial band catalog (switchable in the UI)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit a non-loopback --host; requires QUIETROOM_TOKEN")
    args = parser.parse_args(argv)
    token = os.environ.get("QUIETROOM_TOKEN") or None
    try:
        bind = resolve_bind(args.host, allow_remote=args.allow_remote, token=token)
    except ValueError as exc:
        parser.error(str(exc))
    app, socketio = build_server(demo=args.demo, port=args.port, region=args.region,
                                 bind=bind)
    url = f"http://{args.host}:{args.port}"
    print(f"Quietroom web UI on {url}{'/?token=<QUIETROOM_TOKEN>' if token else ''}"
          f"  (demo={args.demo})")
    _run(socketio, app, args.host, args.port)
    return 0
