"""Fleet client side: node identity, heartbeats, store-and-forward upload.

Transport auth is per-node mutual TLS: each node holds a client certificate
whose identity is its node UUID (SAN ``urn:uuid:<uuid>``, or a UUID CN), the
node pins the central server's CA, and central pins the node CA and binds every
payload to the certificate's UUID (see ``quietroom.central``). The shared
bearer token survives only as an explicitly configured, deprecated fallback.
"""

from __future__ import annotations
import calendar
import functools
import json
import os
import ssl
import sqlite3
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

URN_UUID_PREFIX = "urn:uuid:"
DAY_S = 86400.0
DEFAULT_CERT_WARN_DAYS = 30.0

# The shared bearer token became a deprecated fallback with per-node mTLS (#52).
# It is kept for migration only; the plan below is printed wherever it is used.
LEGACY_TOKEN_DEPRECATED_AT = float(calendar.timegm((2026, 10, 22, 0, 0, 0)))
LEGACY_TOKEN_SUNSET = float(calendar.timegm((2027, 4, 1, 0, 0, 0)))
LEGACY_TOKEN_REMOVAL = "removal planned for quietroom 0.5.0, not before 2027-04-01"
LEGACY_TOKEN_DOCS = (
    "https://github.com/clorth0/quietroom/blob/main/packaging/fleet-mtls.md"
    "#migration-from-the-shared-token"
)

PeerCert = Mapping[str, Any]


def _as_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def identity_from_peer_cert(cert: PeerCert | None) -> str | None:
    """Node UUID from an ``SSLSocket.getpeercert()`` dict, or None if absent/ambiguous.

    Prefers SAN URIs ``urn:uuid:<uuid>``; falls back to a CN that parses as a
    UUID. Several distinct UUIDs, or a UUID CN contradicting the SAN, is None:
    identity must be unambiguous.
    """
    if not cert:
        return None
    sans = {
        _as_uuid(v[len(URN_UUID_PREFIX) :])
        for kind, v in cert.get("subjectAltName", ())
        if kind == "URI" and v.lower().startswith(URN_UUID_PREFIX)
    }
    cns = {
        _as_uuid(v)
        for rdn in cert.get("subject", ())
        for k, v in rdn
        if k == "commonName"
    }
    cn_uuids = {c for c in cns if c is not None}
    if None in sans or len(sans) > 1 or len(cn_uuids) > 1:
        return None
    if sans:
        (san,) = sans
        return san if not cn_uuids or cn_uuids == {san} else None
    return next(iter(cn_uuids)) if cn_uuids else None


def cert_file_identity(path: str | os.PathLike[str]) -> str | None:
    """Node UUID of a PEM certificate on disk (startup self-check).

    The stdlib has no public certificate parser; ``ssl._ssl._test_decode_cert``
    is the decoder behind ``getpeercert()``. If a future CPython drops it this
    raises NotImplementedError and callers skip the self-check (central still
    enforces the binding on every request).
    """
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        raise NotImplementedError("this Python cannot decode certificate files")
    return identity_from_peer_cert(decode(os.fspath(path)))


def cert_file_not_after(path: str | os.PathLike[str]) -> float:
    """``notAfter`` of a PEM certificate on disk, as a Unix timestamp.

    Same stdlib decoder caveat as ``cert_file_identity`` (NotImplementedError).
    """
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        raise NotImplementedError("this Python cannot decode certificate files")
    return float(ssl.cert_time_to_seconds(decode(os.fspath(path))["notAfter"]))


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def cert_expiry(not_after: float, now: float | None = None, warn_s: float = 30 * DAY_S) -> dict:
    """Expiry status of a certificate; ``warn_s`` <= 0 never flags a valid cert as expiring."""
    now = time.time() if now is None else now
    remaining = not_after - now
    expired = remaining < 0
    return {
        "not_after": _utc(not_after),
        "expires_in_s": int(remaining),
        "expiring": expired or (warn_s > 0 and remaining <= warn_s),
        "expired": expired,
    }


CERT_EXPIRY_WARNING_PREFIXES = ("client certificate EXPIRED", "client certificate expires in")


def cert_expiry_warning(status: dict) -> str | None:
    """Operator-facing warning for a ``cert_expiry`` status, or None if nothing to say."""
    if status["expired"]:
        return (
            f"client certificate EXPIRED at {status['not_after']}: central will refuse the "
            "TLS handshake; issue a new certificate for this node"
        )
    if status["expiring"]:
        days = int(status["expires_in_s"] // DAY_S)
        return (
            f"client certificate expires in {days} days ({status['not_after']}); "
            "issue a new certificate for this node before then"
        )
    return None


def client_tls_context(
    cert_file: str | os.PathLike[str],
    key_file: str | os.PathLike[str],
    ca_file: str | os.PathLike[str],
) -> ssl.SSLContext:
    """Node-side mTLS context: presents the node cert and trusts only ``ca_file``."""
    # cafile given => create_default_context loads no system roots (CA pinning).
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=os.fspath(ca_file))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.fspath(cert_file), os.fspath(key_file))
    return ctx


@dataclass
class NodeIdentity:
    node_id: str
    site: str = ""
    floor: str = ""
    room: str = ""
    version: str = ""
    config_hash: str = ""

    @classmethod
    def load_or_create(cls, path, **meta):
        p = Path(path)
        if p.exists():
            d = json.loads(p.read_text())
            return cls(**{**d, **{k: v for k, v in meta.items() if v}})
        ident = cls(node_id=meta.pop("node_id", str(uuid.uuid4())), **meta)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(ident), indent=2))
        return ident


def heartbeat_payload(ident, health, now=None):
    return {
        "node_id": ident.node_id,
        "site": ident.site,
        "floor": ident.floor,
        "room": ident.room,
        "version": ident.version,
        "config_hash": ident.config_hash,
        "timestamp": time.time() if now is None else now,
        "baselines": health.get("baselines", []),
        "segments": health.get("last_run", {}),
        "last_error": health.get("last_error"),
        # learning safety rails: anchor divergence, incident holds, waiting refreshes
        "anchors": health.get("anchors", {}),
        "incident_hold": health.get("incident_hold", {}),
        "rebaseline_deferred": health.get("rebaseline_deferred", {}),
        # alert delivery: queued, dead-lettered, and why the last flush failed
        "alert_outbox": health.get("alert_outbox", {}),
        "alert_parked": health.get("alert_parked", {}),
        "alert_delivery_errors": health.get("alert_delivery_errors", {}),
        # fleet identity: client-certificate expiry
        "fleet_cert": health.get("fleet_cert"),
    }


Sender = Callable[[str, bytes, dict[str, str]], object]


def _post(
    url: str, body: bytes, headers: dict[str, str], context: ssl.SSLContext | None = None
) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # operator-configured central URL
    with urllib.request.urlopen(req, timeout=10, context=context) as r:  # noqa: S310
        return int(r.status)


class FleetClient:
    """HTTPS push client with a SQLite store-and-forward outbox.

    With ``ssl_context`` (see ``client_tls_context``) the node authenticates by
    client certificate and stamps ``node_uuid`` into every payload; central
    rejects payloads whose UUID differs from the certificate's. ``token`` is the
    deprecated shared bearer and is only sent when explicitly given.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        sender: Sender | None = None,
        outbox_path: str | os.PathLike[str] = ":memory:",
        *,
        ssl_context: ssl.SSLContext | None = None,
        node_uuid: str | None = None,
        cert_not_after: float | None = None,
        cert_warn_s: float = DEFAULT_CERT_WARN_DAYS * DAY_S,
    ):
        self.base = base_url.rstrip("/")
        if ssl_context is not None and not self.base.lower().startswith("https://"):
            raise ValueError(f"mTLS fleet client needs an https:// base_url, got {base_url!r}")
        if node_uuid is not None and _as_uuid(node_uuid) != node_uuid:
            raise ValueError(f"node_uuid must be a canonical UUID, got {node_uuid!r}")
        self.token = token
        self.ssl_context = ssl_context
        self.node_uuid = node_uuid
        self.cert_not_after = cert_not_after
        self.cert_warn_s = cert_warn_s
        self.sender: Sender = sender or functools.partial(_post, context=ssl_context)
        self.db = sqlite3.connect(str(outbox_path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fleet_outbox("
            "id INTEGER PRIMARY KEY, path TEXT, payload TEXT)"
        )
        self.db.commit()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _send(self, path, payload):
        if self.node_uuid is not None:
            payload = {**payload, "node_uuid": self.node_uuid}
        body = json.dumps(payload, sort_keys=True).encode()
        if self.pending():
            # Never overtake the backlog, and don't spend another network timeout on a
            # central we already know is failing: queue behind it; flush() drains in order.
            self._enqueue(path, body)
            return False
        try:
            self.sender(f"{self.base}{path}", body, self._headers())
            return True
        except Exception:
            self._enqueue(path, body)
            return False

    def _enqueue(self, path: str, body: bytes) -> None:
        self.db.execute("INSERT INTO fleet_outbox(path,payload) VALUES(?,?)", (path, body.decode()))
        self.db.commit()

    @property
    def auth_mode(self) -> str:
        if self.ssl_context is not None and self.node_uuid is not None:
            return "mtls"
        return "legacy-token" if self.token else "none"

    def cert_status(self, now: float | None = None) -> dict | None:
        """Expiry of this node's client certificate (None without mTLS or if unknown)."""
        if self.cert_not_after is None:
            return None
        return cert_expiry(self.cert_not_after, now, self.cert_warn_s)

    def send_heartbeat(self, payload):
        return self._send("/api/v1/heartbeat", payload)

    def send_events(self, node_id, events):
        return self._send("/api/v1/events", {"node_id": node_id, "events": events})

    def pending(self):
        return self.db.execute("SELECT COUNT(*) FROM fleet_outbox").fetchone()[0]

    def flush(self):
        sent = 0
        for row_id, path, payload in self.db.execute(
            "SELECT id,path,payload FROM fleet_outbox ORDER BY id"
        ).fetchall():
            try:
                self._send_direct(path, payload)
            except Exception:
                break  # central unreachable/refusing: keep order, retry next flush
            self.db.execute("DELETE FROM fleet_outbox WHERE id=?", (row_id,))
            sent += 1
        self.db.commit()
        return sent

    def _send_direct(self, path, payload):
        self.sender(f"{self.base}{path}", payload.encode(), self._headers())
