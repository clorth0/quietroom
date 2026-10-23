"""Minimal central aggregation service: node registry, heartbeats, event ingest.

This is the fleet counterpart to quietroom.fleet.

Auth model (``require_client_cert=True``, the deployment mode):

* Nodes authenticate by **mutual TLS**. The TLS listener (``make_central_server``
  with ``server_tls_context``) only trusts the pinned node CA, and the verified
  peer certificate is placed in the WSGI environ under ``PEER_CERT_ENVIRON`` by
  our request handler — never derived from HTTP headers, which cannot write
  that key. The node's identity is the certificate's UUID (SAN
  ``urn:uuid:<uuid>`` or UUID CN), and every write must carry a matching
  ``node_uuid``; the record is keyed and stamped with the certificate's UUID,
  not anything the body claims.
* ``token`` is the deprecated shared node bearer. If given, certificate-less
  writes bearing it are still accepted during migration, stamped
  ``auth: "legacy-token"`` and answered with ``X-Quietroom-Auth: legacy-token``.
* Reads (``GET /api/v1/*``) are for operators: they need ``read_token``. A node
  certificate or the legacy node token does not grant fleet-wide reads.

``require_client_cert=False`` keeps the pre-mTLS behaviour (one shared token for
everything, or no auth at all when ``token`` is None) for tests and local use.

State is in-memory by design for this MVP — the Store schema is the persistence
path when central grows up.
"""

from __future__ import annotations
import argparse
import hmac
import json
import os
import ssl
import sys
import time
from typing import Any

from flask import Flask, abort, g, request
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

from quietroom.fleet import identity_from_peer_cert

PEER_CERT_ENVIRON = "quietroom.peer_cert"
LEGACY_TOKEN_ENV = "QUIETROOM_CENTRAL_TOKEN"
READ_TOKEN_ENV = "QUIETROOM_CENTRAL_READ_TOKEN"


def _bearer_ok(expected: str | None) -> bool:
    if not expected:
        return False
    given = request.headers.get("Authorization", "")
    return hmac.compare_digest(given.encode(), f"Bearer {expected}".encode())


def create_central_app(
    token: str | None = None,
    stale_after_s: float = 180,
    *,
    require_client_cert: bool = False,
    read_token: str | None = None,
) -> Flask:
    app = Flask(__name__)
    nodes: dict[str, dict[str, Any]] = {}  # node_uuid (mTLS) or node_id (legacy) -> heartbeat
    events: list[dict[str, Any]] = []
    app.central_nodes = nodes  # type: ignore[attr-defined]
    app.central_events = events  # type: ignore[attr-defined]

    @app.before_request
    def _auth():
        g.node_uuid = None
        g.auth = None
        if request.path == "/healthz":
            return None
        if not require_client_cert:
            if token is not None and not _bearer_ok(token):
                abort(403)
            g.auth = "legacy-token" if token is not None else "none"
            return None
        if request.method == "GET":
            if not _bearer_ok(read_token):
                abort(403)
            g.auth = "operator"
            return None
        peer = request.environ.get(PEER_CERT_ENVIRON)
        if peer:
            ident = identity_from_peer_cert(peer)
            if ident is None:
                abort(403, "client certificate carries no unambiguous node UUID")
            body = request.get_json(force=True, silent=True) or {}
            if body.get("node_uuid") != ident:
                abort(403, "payload node_uuid does not match the client certificate")
            g.node_uuid, g.auth = ident, "mtls"
            return None
        if token is not None and _bearer_ok(token):
            g.auth = "legacy-token"
            return None
        abort(403)

    @app.after_request
    def _flag(resp):
        if g.get("auth") == "legacy-token":
            resp.headers["X-Quietroom-Auth"] = "legacy-token"
        return resp

    def _bound(d: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"node_id": d.get("node_id"), "auth": g.auth}
        if g.node_uuid is not None:
            out["node_uuid"] = g.node_uuid
        return out

    @app.post("/api/v1/heartbeat")
    def heartbeat():
        d = request.get_json(force=True)
        rec = {**d, **_bound(d)}
        if g.node_uuid is None:
            rec.pop("node_uuid", None)  # unauthenticated claim; never stored
        nodes[g.node_uuid or str(d["node_id"])] = rec
        return {"ok": True, "nodes": len(nodes)}

    @app.post("/api/v1/events")
    def ingest():
        d = request.get_json(force=True)
        bound = _bound(d)
        for e in d.get("events", []):
            rec = {**e, **bound}
            if g.node_uuid is None:
                rec.pop("node_uuid", None)  # unauthenticated claim; never stored
            events.append(rec)
        return {"ok": True, "stored": len(events)}

    @app.get("/api/v1/nodes")
    def list_nodes():
        now = time.time()
        out = [{**n, "stale": now - n.get("timestamp", 0) > stale_after_s} for n in nodes.values()]
        return {"nodes": out}

    @app.get("/api/v1/events")
    def list_events():
        node = request.args.get("node_id")
        node_uuid = request.args.get("node_uuid")
        return {
            "events": [
                e
                for e in events
                if (node is None or e.get("node_id") == node)
                and (node_uuid is None or e.get("node_uuid") == node_uuid)
            ]
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "nodes": len(nodes), "events": len(events)}

    return app


def server_tls_context(
    cert_file: str | os.PathLike[str],
    key_file: str | os.PathLike[str],
    client_ca_file: str | os.PathLike[str],
    *,
    require_client_cert: bool = True,
) -> ssl.SSLContext:
    """Central listener context trusting *only* the node CA for client certs.

    ``require_client_cert=False`` (CERT_OPTIONAL) exists solely so certificate-
    less legacy-token nodes can still connect during migration.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.fspath(cert_file), os.fspath(key_file))
    ctx.load_verify_locations(cafile=os.fspath(client_ca_file))  # no system roots: pinned
    ctx.verify_mode = ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_OPTIONAL
    return ctx


class PeerCertRequestHandler(WSGIRequestHandler):
    """Expose the verified TLS client certificate to the app via the WSGI environ."""

    def make_environ(self):
        environ = super().make_environ()
        getpeercert = getattr(self.connection, "getpeercert", None)
        environ[PEER_CERT_ENVIRON] = getpeercert() if getpeercert is not None else None
        return environ


def make_central_server(
    app: Flask, host: str, port: int, ssl_context: ssl.SSLContext
) -> BaseWSGIServer:
    return make_server(
        host,
        port,
        app,
        threaded=True,
        request_handler=PeerCertRequestHandler,
        ssl_context=ssl_context,
    )


def build_central(
    cert: str, key: str, client_ca: str, *, allow_legacy_token: bool, env: dict[str, str],
    stale_after_s: float = 180,
) -> tuple[Flask, ssl.SSLContext, str]:
    """App + TLS context for ``quietroom-central``; returns a human-readable mode too.

    Client certificates are mandatory *at the TLS layer* unless something
    legitimately connects without one: legacy-token nodes during migration, or
    operators reading with ``QUIETROOM_CENTRAL_READ_TOKEN``. Either way the app
    refuses every certificate-less write that lacks the legacy token.
    """
    legacy = env.get(LEGACY_TOKEN_ENV) if allow_legacy_token else None
    if allow_legacy_token and not legacy:
        raise SystemExit(f"--allow-legacy-token needs ${LEGACY_TOKEN_ENV} in the environment")
    read_token = env.get(READ_TOKEN_ENV) or None
    app = create_central_app(
        legacy, stale_after_s, require_client_cert=True, read_token=read_token
    )
    certless = legacy is not None or read_token is not None
    ctx = server_tls_context(cert, key, client_ca, require_client_cert=not certless)
    parts = ["node writes: mTLS"]
    if legacy:
        parts.append("DEPRECATED legacy token")
    parts.append("operator reads: token" if read_token else "operator reads: disabled")
    parts.append("client cert optional at TLS" if certless else "client cert required at TLS")
    return app, ctx, "; ".join(parts)


def check_central_config(
    cert: str, key: str, client_ca: str, *, allow_legacy_token: bool, env: dict[str, str]
) -> dict[str, Any]:
    """Side-effect-free audit for ``quietroom-central --check-config``."""
    from quietroom.checks import Report, present
    from quietroom.update import check_secret_file

    r = Report()
    if allow_legacy_token:
        r.secret(
            env, LEGACY_TOKEN_ENV, "legacy shared bearer token (certificate-less nodes)",
            required=True, missing=f"--allow-legacy-token needs ${LEGACY_TOKEN_ENV}",
        )
    elif present(env, LEGACY_TOKEN_ENV):
        r.warnings.append(
            f"${LEGACY_TOKEN_ENV} is set but ignored without --allow-legacy-token; "
            "remove it from central's environment"
        )
    r.secret(
        env, READ_TOKEN_ENV, "operator reads (GET /api/v1/...)", required=False,
        missing=f"${READ_TOKEN_ENV} is not set: operator reads are disabled "
        "(every GET is refused)",
    )
    missing = [p for p in (cert, key, client_ca) if not os.access(p, os.R_OK)]
    if missing:
        r.check("TLS material loads", False, f"missing or unreadable: {', '.join(missing)}")
    else:
        try:
            server_tls_context(cert, key, client_ca)
        except (OSError, ValueError) as e:
            r.check("TLS material loads", False, f"cannot load TLS material: {e}")
        else:
            r.check("TLS material loads", True)
            r.warnings += check_secret_file(key)
    return r.as_dict()


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    p = argparse.ArgumentParser(
        prog="quietroom-central", description="Quietroom fleet central ingest (mTLS)"
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--cert", required=True, help="central server certificate (PEM)")
    p.add_argument("--key", required=True, help="central server private key (PEM)")
    p.add_argument("--client-ca", required=True, help="CA that issues node certificates")
    p.add_argument(
        "--allow-legacy-token",
        action="store_true",
        help=f"DEPRECATED: also accept certificate-less nodes bearing ${LEGACY_TOKEN_ENV}",
    )
    p.add_argument("--stale-after-s", type=float, default=180)
    p.add_argument(
        "--check-config",
        action="store_true",
        help="check TLS material and env tokens (values never printed); exit 1 on errors",
    )
    a = p.parse_args(argv)
    if a.check_config:
        report = check_central_config(
            a.cert, a.key, a.client_ca, allow_legacy_token=a.allow_legacy_token, env=env
        )
        print(json.dumps(report, indent=2))
        for e in report["errors"]:
            print(f"ERROR: {e}", file=sys.stderr)
        for w in report["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        return 0 if report["ok"] else 1
    app, ctx, mode = build_central(
        a.cert,
        a.key,
        a.client_ca,
        allow_legacy_token=a.allow_legacy_token,
        env=env,
        stale_after_s=a.stale_after_s,
    )
    srv = make_central_server(app, a.host, a.port, ctx)
    print(f"quietroom-central listening on https://{a.host}:{srv.server_port} ({mode})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
