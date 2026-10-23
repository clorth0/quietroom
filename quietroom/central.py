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
* Revocation: an optional ``DenyList`` file of node UUIDs is checked on every
  write and re-read when it changes, so revoking a node needs no restart. If
  the file becomes unreadable or malformed, *all* writes get 503 (fail closed)
  until it is fixed; nodes keep their data in their outboxes meanwhile.

``require_client_cert=False`` keeps the pre-mTLS behaviour (one shared token for
everything, or no auth at all when ``token`` is None) for tests and local use.

State is in-memory by design for this MVP — the Store schema is the persistence
path when central grows up.
"""

from __future__ import annotations
import argparse
import email.utils
import hmac
import json
import os
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, request
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

from quietroom.fleet import (
    DAY_S,
    DEFAULT_CERT_WARN_DAYS,
    LEGACY_TOKEN_DEPRECATED_AT,
    LEGACY_TOKEN_DOCS,
    LEGACY_TOKEN_REMOVAL,
    LEGACY_TOKEN_SUNSET,
    cert_expiry,
    cert_expiry_warning,
    identity_from_peer_cert,
)

PEER_CERT_ENVIRON = "quietroom.peer_cert"
LEGACY_TOKEN_ENV = "QUIETROOM_CENTRAL_TOKEN"
READ_TOKEN_ENV = "QUIETROOM_CENTRAL_READ_TOKEN"


def _warn(msg: str) -> None:
    print(f"quietroom-central: WARNING: {msg}", file=sys.stderr, flush=True)


CERT_WARN_DAYS_MAX = 365


def parse_cert_warn_days(value: object) -> int:
    """``--cert-warn-days``: a whole number of days from 1 to CERT_WARN_DAYS_MAX."""
    allowed = f"an integer from 1 to {CERT_WARN_DAYS_MAX} (days)"
    if isinstance(value, bool):
        raise ValueError(f"--cert-warn-days must be {allowed}, got {value!r}")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        n = int(value.strip())
    else:
        raise ValueError(f"--cert-warn-days must be {allowed}, got {value!r}")
    if not 1 <= n <= CERT_WARN_DAYS_MAX:
        raise ValueError(f"--cert-warn-days must be {allowed}, got {n}")
    return n


def check_deny_list(path: str | os.PathLike[str]) -> int:
    """Number of revoked UUIDs in a ``--deny-list`` file; ValueError naming the problem."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"--deny-list {p}: no such file") from None
    except IsADirectoryError:
        raise ValueError(f"--deny-list {p}: is a directory, not a file") from None
    except UnicodeDecodeError:
        raise ValueError(f"--deny-list {p}: not UTF-8 text") from None
    except OSError as e:
        raise ValueError(f"--deny-list {p}: unreadable: {e.strerror}") from None
    try:
        return len(parse_deny_list(text))
    except ValueError as e:
        raise ValueError(f"--deny-list {p}: {e}") from None


def parse_deny_list(text: str) -> frozenset[str]:
    """Node UUIDs, one per line; ``#`` starts a comment. Any other content is an error."""
    out: set[str] = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            out.add(str(uuid.UUID(line)))
        except ValueError:
            raise ValueError(f"line {n}: not a node UUID: {line!r}") from None
    return frozenset(out)


class DenyListUnavailable(Exception):
    pass


class DenyList:
    """A revocation file re-read whenever it changes (checked by ``stat`` per request).

    Construction is strict (a missing or malformed file raises ValueError) so a
    typo cannot start central with revocation silently off. After that, a file
    that disappears or stops parsing makes ``current()`` raise
    ``DenyListUnavailable`` rather than fall back to an older list: a revocation
    that was being added must not be silently ignored.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sig: tuple[int, ...] | None = None
        self._uuids: frozenset[str] = frozenset()
        self.error: str | None = None
        self._refresh()
        if self.error:
            raise ValueError(self.error)

    def _read(self) -> frozenset[str]:
        return parse_deny_list(self.path.read_text(encoding="utf-8"))

    def _refresh(self) -> None:
        try:
            st = os.stat(self.path)
            sig: tuple[int, ...] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        except OSError as e:
            self._sig, self.error = None, f"deny list {self.path} unreadable: {e.strerror}"
            return
        if sig == self._sig:
            return
        try:
            uuids = self._read()
        except (OSError, UnicodeDecodeError, ValueError) as e:
            self._sig, self.error = None, f"deny list {self.path} rejected: {e}"
            return
        self._sig, self._uuids, self.error = sig, uuids, None

    def current(self) -> frozenset[str]:
        with self._lock:
            self._refresh()
            if self.error:
                raise DenyListUnavailable(self.error)
            return self._uuids

    def last_good(self) -> frozenset[str]:
        with self._lock:
            self._refresh()
            return self._uuids

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return {"path": str(self.path), "entries": len(self._uuids), "error": self.error}


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
    deny_list: DenyList | None = None,
    cert_warn_days: float = DEFAULT_CERT_WARN_DAYS,
) -> Flask:
    if deny_list is not None and not require_client_cert:
        raise ValueError("a deny list identifies nodes by client certificate UUID; it needs "
                         "require_client_cert=True")
    app = Flask(__name__)
    nodes: dict[str, dict[str, Any]] = {}  # node_uuid (mTLS) or node_id (legacy) -> heartbeat
    events: list[dict[str, Any]] = []
    cert_not_after: dict[str, float] = {}  # node key -> notAfter of the verified TLS cert
    warned_expiry: set[str] = set()
    warned_revoked: set[str] = set()
    counters = {"legacy_token_writes": 0}
    warn_s = cert_warn_days * DAY_S
    app.central_nodes = nodes  # type: ignore[attr-defined]
    app.central_events = events  # type: ignore[attr-defined]

    def _cert(key: str, now: float) -> dict[str, Any] | None:
        na = cert_not_after.get(key)
        return None if na is None else cert_expiry(na, now, warn_s)

    @app.before_request
    def _auth():
        g.node_uuid = None
        g.auth = None
        g.not_after = None
        if request.path == "/healthz":
            return None
        denied: frozenset[str] = frozenset()
        if deny_list is not None and request.method != "GET":
            try:
                denied = deny_list.current()
            except DenyListUnavailable as e:
                abort(503, f"{e}; writes are paused until the deny list is fixed")
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
            if ident in denied:
                if ident not in warned_revoked:
                    warned_revoked.add(ident)
                    _warn(f"refusing writes from revoked node {ident} (deny list)")
                abort(403, "node certificate revoked (central deny list)")
            g.node_uuid, g.auth = ident, "mtls"
            if peer.get("notAfter"):
                g.not_after = float(ssl.cert_time_to_seconds(peer["notAfter"]))
            return None
        if token is not None and _bearer_ok(token):
            g.auth = "legacy-token"
            return None
        abort(403)

    @app.after_request
    def _flag(resp):
        if g.get("auth") == "legacy-token":
            resp.headers["X-Quietroom-Auth"] = "legacy-token"
            resp.headers["Deprecation"] = f"@{int(LEGACY_TOKEN_DEPRECATED_AT)}"  # RFC 9745
            resp.headers["Sunset"] = email.utils.formatdate(LEGACY_TOKEN_SUNSET, usegmt=True)
            resp.headers["Link"] = f'<{LEGACY_TOKEN_DOCS}>; rel="deprecation"'
            if request.method != "GET" and resp.status_code < 400:
                counters["legacy_token_writes"] += 1
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
        key = g.node_uuid or str(d["node_id"])
        nodes[key] = rec
        if g.not_after is not None:
            cert_not_after[key] = g.not_after
            status = cert_expiry(g.not_after, warn_s=warn_s)
            msg = cert_expiry_warning(status)
            if msg and key not in warned_expiry:
                warned_expiry.add(key)
                _warn(f"node {key} ({rec.get('node_id')}): {msg}")
        else:
            cert_not_after.pop(key, None)
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
        revoked = deny_list.last_good() if deny_list is not None else frozenset()
        out = [
            {
                **n,
                "stale": now - n.get("timestamp", 0) > stale_after_s,
                "cert": _cert(key, now),  # from the TLS handshake; never the body's claim
                "revoked": key in revoked,
            }
            for key, n in nodes.items()
        ]
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
        now = time.time()
        deny = deny_list.status() if deny_list is not None else None
        expiring = sum(1 for k in nodes if (_cert(k, now) or {}).get("expiring"))
        return {
            "ok": deny is None or deny["error"] is None,
            "nodes": len(nodes),
            "events": len(events),
            "certs_expiring": expiring,
            "deny_list": deny,
            "legacy_token_writes": counters["legacy_token_writes"],
        }

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
    deny_list: str | None = None,
    cert_warn_days: float = DEFAULT_CERT_WARN_DAYS,
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
    deny: DenyList | None = None
    if deny_list:
        try:
            deny = DenyList(deny_list)
        except ValueError as e:
            raise SystemExit(f"--deny-list: {e}") from e
    if legacy:
        _warn(
            f"--allow-legacy-token is DEPRECATED ({LEGACY_TOKEN_REMOVAL}): any holder of "
            f"${LEGACY_TOKEN_ENV} can post as any node, and the deny list cannot revoke it. "
            "Migrate nodes to mTLS: packaging/fleet-mtls.md"
        )
    app = create_central_app(
        legacy,
        stale_after_s,
        require_client_cert=True,
        read_token=read_token,
        deny_list=deny,
        cert_warn_days=cert_warn_days,
    )
    certless = legacy is not None or read_token is not None
    ctx = server_tls_context(cert, key, client_ca, require_client_cert=not certless)
    parts = ["node writes: mTLS"]
    if legacy:
        parts.append("DEPRECATED legacy token")
    parts.append("operator reads: token" if read_token else "operator reads: disabled")
    if deny is not None:
        parts.append(f"deny list: {deny.status()['entries']} revoked ({deny.path})")
    parts.append("client cert optional at TLS" if certless else "client cert required at TLS")
    return app, ctx, "; ".join(parts)


def check_central_config(
    cert: str, key: str, client_ca: str, *, allow_legacy_token: bool, env: dict[str, str],
    deny_list: str | None = None,
    cert_warn_days: object = int(DEFAULT_CERT_WARN_DAYS),
) -> dict[str, Any]:
    """Side-effect-free audit for ``quietroom-central --check-config``.

    Besides TLS material and tokens: the ``--deny-list`` file (exists, UTF-8,
    one node UUID per line) and ``--cert-warn-days`` (see parse_cert_warn_days).
    """
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
    if deny_list:
        try:
            n = check_deny_list(deny_list)
        except ValueError as e:
            r.check("deny list loads", False, str(e))
        else:
            r.check("deny list loads", True, detail=f"{n} revoked ({deny_list})")
    try:
        parse_cert_warn_days(cert_warn_days)
    except ValueError as e:
        r.check("--cert-warn-days in range", False, str(e))
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
    p.add_argument(
        "--deny-list",
        metavar="FILE",
        help="revoked node UUIDs, one per line (# comments); re-read on change, no restart",
    )
    p.add_argument(
        "--cert-warn-days",
        default=str(int(DEFAULT_CERT_WARN_DAYS)),
        help="flag node client certificates expiring within this many days "
        f"(an integer, 1-{CERT_WARN_DAYS_MAX}; default %(default)s)",
    )
    a = p.parse_args(argv)
    if a.check_config:
        report = check_central_config(
            a.cert, a.key, a.client_ca, allow_legacy_token=a.allow_legacy_token, env=env,
            deny_list=a.deny_list, cert_warn_days=a.cert_warn_days,
        )
        print(json.dumps(report, indent=2))
        for e in report["errors"]:
            print(f"ERROR: {e}", file=sys.stderr)
        for w in report["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        return 0 if report["ok"] else 1
    try:
        cert_warn_days = parse_cert_warn_days(a.cert_warn_days)
    except ValueError as e:
        p.error(str(e))
    app, ctx, mode = build_central(
        a.cert,
        a.key,
        a.client_ca,
        allow_legacy_token=a.allow_legacy_token,
        env=env,
        stale_after_s=a.stale_after_s,
        deny_list=a.deny_list,
        cert_warn_days=cert_warn_days,
    )
    srv = make_central_server(app, a.host, a.port, ctx)
    print(f"quietroom-central listening on https://{a.host}:{srv.server_port} ({mode})", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
