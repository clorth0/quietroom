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

from quietroom.sqlitedb import Schema, open_db

URN_UUID_PREFIX = "urn:uuid:"
HEARTBEAT_PATH = "/api/v1/heartbeat"
# Outbox bounds (defaults of the [fleet] outbox_* config keys).
OUTBOX_MAX_ROWS = 20_000
OUTBOX_MAX_AGE_S = 7 * 86400.0
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BUSY_TIMEOUT_S = 30.0
# Rows delivered per flush (per tick): a long backlog drains over several ticks
# instead of holding one tick past the systemd watchdog.
FLUSH_BATCH = 500
_OUTBOX_COLUMNS = {
    "created_at": "REAL",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT NOT NULL DEFAULT ''",
    "last_attempt_at": "REAL",
    "parked_at": "REAL",
    "park_reason": "TEXT",
}
FLEET_OUTBOX_SCHEMA = Schema(
    "fleet outbox",
    statements=(
        "CREATE TABLE IF NOT EXISTS fleet_outbox(id INTEGER PRIMARY KEY, path TEXT, payload TEXT)",
        "CREATE TABLE IF NOT EXISTS fleet_outbox_meta(key TEXT PRIMARY KEY, value INTEGER)",
    ),
    columns={"fleet_outbox": _OUTBOX_COLUMNS},
)
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
        # fleet upload backlog: queued, set aside, deleted past the bound
        "fleet_outbox": health.get("fleet_outbox", {}),
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
    """HTTPS push client with a bounded SQLite store-and-forward outbox.

    Undelivered uploads queue in order and are retried by :meth:`flush`. The
    queue is bounded and sets aside what it cannot deliver, like the alert
    outbox (``quietroom.alerts.Outbox``) rather than growing forever:

    * queued heartbeats coalesce: central keeps only the latest per node, so
      only the newest queued one is kept;
    * ``max_rows``: past this many queued rows the oldest are *parked*
      ("overflow"); past the same number of parked rows the oldest parked are
      deleted and counted in :meth:`dropped`, so the file holds at most
      ``2 * max_rows`` rows;
    * ``max_age_s``: rows undelivered this long are parked ("stale"); 0 = never;
    * ``max_attempts``: a row that failed this many flushes is a *suspect* and
      stops holding the queue; it is parked as "poison" only if it fails again
      in a flush where central accepted other rows. An outage (nothing gets
      through) never parks anything, and costs one network attempt per flush.

    Parked rows stay on disk for ``quietroom-node --outbox list|requeue
    --channel fleet`` (see :func:`fleet_outbox_rows`, :func:`requeue_fleet_parked`).

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
        max_rows: int = OUTBOX_MAX_ROWS,
        max_age_s: float = OUTBOX_MAX_AGE_S,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
        clock: Callable[[], float] = time.time,
        flush_batch: int = FLUSH_BATCH,
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
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError(f"fleet outbox max_rows must be an integer >= 1, got {max_rows!r}")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 0:
            raise ValueError(
                f"fleet outbox max_attempts must be an integer >= 0, got {max_attempts!r}"
            )
        if isinstance(flush_batch, bool) or not isinstance(flush_batch, int) or flush_batch < 1:
            raise ValueError(
                f"fleet outbox flush_batch must be an integer >= 1, got {flush_batch!r}"
            )
        if not max_age_s >= 0:
            raise ValueError(f"fleet outbox max_age_s must be >= 0, got {max_age_s!r}")
        self.sender: Sender = sender or functools.partial(_post, context=ssl_context)
        self.max_rows = max_rows
        self.max_age_s = float(max_age_s)
        self.max_attempts = max_attempts
        self.clock = clock
        self.flush_batch = flush_batch
        # Rows set aside since the consumer (the service) last read and cleared this.
        self.last_parked: list[dict[str, Any]] = []
        self.db = _outbox_db(outbox_path, self.clock())

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
        now = self.clock()
        if path == HEARTBEAT_PATH:
            # Central overwrites a node's record with each heartbeat: an older
            # queued one carries nothing the newer one does not.
            self.db.execute(
                "DELETE FROM fleet_outbox WHERE path=? AND parked_at IS NULL", (HEARTBEAT_PATH,)
            )
        self.db.execute(
            "INSERT INTO fleet_outbox(path,payload,created_at) VALUES(?,?,?)",
            (path, body.decode(), now),
        )
        self._bound(now)
        self.db.commit()

    def _bound(self, now: float) -> None:
        """Hold the queue to max_rows live rows plus max_rows parked ones."""
        over = self.pending() - self.max_rows
        if over > 0:
            rows = self.db.execute(
                "SELECT id,path FROM fleet_outbox WHERE parked_at IS NULL ORDER BY id LIMIT ?",
                (over,),
            ).fetchall()
            reason = f"overflow: more than {self.max_rows} undelivered rows (outbox_max_rows)"
            for row_id, path in rows:
                self._park(row_id, path, reason, now)
        extra = self.parked() - self.max_rows
        if extra > 0:
            self.db.execute(
                "DELETE FROM fleet_outbox WHERE id IN (SELECT id FROM fleet_outbox "
                "WHERE parked_at IS NOT NULL ORDER BY id LIMIT ?)",
                (extra,),
            )
            self.db.execute(
                "INSERT INTO fleet_outbox_meta(key,value) VALUES('dropped',?) "
                "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                (extra,),
            )

    def _park(self, row_id: int, path: str, reason: str, now: float) -> None:
        self.db.execute(
            "UPDATE fleet_outbox SET parked_at=?, park_reason=? WHERE id=?", (now, reason, row_id)
        )
        self.last_parked.append({"id": row_id, "path": path, "reason": reason})
        del self.last_parked[:-100]

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
        return self._send(HEARTBEAT_PATH, payload)

    def send_events(self, node_id, events):
        return self._send("/api/v1/events", {"node_id": node_id, "events": events})

    def pending(self) -> int:
        """Rows still being delivered (not parked)."""
        return self.db.execute(
            "SELECT COUNT(*) FROM fleet_outbox WHERE parked_at IS NULL"
        ).fetchone()[0]

    def parked(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM fleet_outbox WHERE parked_at IS NOT NULL"
        ).fetchone()[0]

    def dropped(self) -> int:
        """Parked rows deleted to respect max_rows, ever (persisted)."""
        row = self.db.execute("SELECT value FROM fleet_outbox_meta WHERE key='dropped'").fetchone()
        return int(row[0]) if row else 0

    def outbox_status(self) -> dict[str, int]:
        return {"pending": self.pending(), "parked": self.parked(), "dropped": self.dropped()}

    def flush(self) -> int:
        """Deliver queued rows in order; returns how many went out.

        Stops at the first failing row (central down: one attempt per flush),
        except that suspects (>= max_attempts failures) are skipped so one bad
        payload cannot hold the queue. Suspects are retried after the others only
        while the previous attempt succeeded, and parked as poison if they fail
        then. At most ``flush_batch`` rows go out per call.
        """
        now = self.clock()
        if self.max_age_s > 0:
            stale = self.db.execute(
                "SELECT id,path FROM fleet_outbox WHERE parked_at IS NULL AND created_at < ?",
                (now - self.max_age_s,),
            ).fetchall()
            reason = f"stale: undelivered for more than {self.max_age_s:g} s (outbox_max_age_s)"
            for row_id, path in stale:
                self._park(row_id, path, reason, now)
            self.db.commit()
        rows = self.db.execute(
            "SELECT id,path,payload,attempts FROM fleet_outbox WHERE parked_at IS NULL "
            "ORDER BY id"
        ).fetchall()
        sent = 0
        tried = False
        last_ok = False  # did the most recent attempt in this flush succeed?
        suspects = []
        for row in rows:
            if sent >= self.flush_batch:
                return sent
            if self.max_attempts and row[3] >= self.max_attempts:
                suspects.append(row)
                continue
            tried = True
            last_ok = self._attempt(row, now)
            if not last_ok:
                break
            sent += 1
        if not tried and suspects:
            # Everything left is a suspect: probe with the oldest so a recovered
            # central drains the queue (and a dead one still costs one attempt).
            last_ok = self._attempt(suspects.pop(0), now)
            sent += last_ok
        # A suspect is retried only while central is demonstrably up (the attempt
        # just before succeeded); failing then means the payload is the problem.
        # Central going down mid-flush therefore parks nothing.
        for row in suspects:
            if not last_ok or sent >= self.flush_batch:
                break
            if self._attempt(row, now):
                sent += 1
                continue
            self._park(
                row[0], row[1],
                f"poison: failed {row[3] + 1} times while central accepted other rows",
                now,
            )
            self.db.commit()
            last_ok = False  # one park per proof: the next suspect needs a fresh success
        return sent

    def _attempt(self, row: tuple, now: float) -> bool:
        row_id, path, payload, _ = row
        try:
            self._send_direct(path, payload)
        except Exception as e:  # central unreachable/refusing: keep the row
            self.db.execute(
                "UPDATE fleet_outbox SET attempts=attempts+1, last_attempt_at=?, last_error=? "
                "WHERE id=?",
                (now, f"{type(e).__name__}: {e}"[:500], row_id),
            )
            self.db.commit()
            return False
        self.db.execute("DELETE FROM fleet_outbox WHERE id=?", (row_id,))
        self.db.commit()
        return True

    def close(self) -> None:
        self.db.close()

    def _send_direct(self, path, payload):
        self.sender(f"{self.base}{path}", payload.encode(), self._headers())


def _outbox_db(path: str | os.PathLike[str], now: float) -> sqlite3.Connection:
    """Open (creating or upgrading in place) a fleet outbox file; see quietroom.sqlitedb."""

    def backfill(db: sqlite3.Connection) -> None:
        # Rows queued before the upgrade start their age clock now.
        db.execute("UPDATE fleet_outbox SET created_at=? WHERE created_at IS NULL", (now,))

    return open_db(
        path, FLEET_OUTBOX_SCHEMA, busy_timeout_s=OUTBOX_BUSY_TIMEOUT_S, upgrade=backfill
    )


def fleet_outbox_rows(path, *, parked=None):
    """Rows of a fleet outbox file (``quietroom-node --outbox list --channel fleet``).

    parked: None = all, True = parked only, False = still queued.
    """
    db = _outbox_db(path, time.time())
    q = (
        "SELECT id,path,attempts,created_at,last_attempt_at,last_error,parked_at,park_reason,"
        "payload FROM fleet_outbox"
    )
    if parked is not None:
        q += " WHERE parked_at IS NOT NULL" if parked else " WHERE parked_at IS NULL"
    out = []
    try:
        for r in db.execute(q + " ORDER BY id"):
            try:
                payload = json.loads(r[8])
            except (TypeError, ValueError):
                payload = r[8]
            out.append(
                {
                    "id": r[0], "channel": "fleet", "path": r[1], "attempts": r[2],
                    "created_at": r[3], "last_attempt_at": r[4], "last_error": r[5] or "",
                    "parked_at": r[6], "park_reason": r[7], "payload": payload,
                }
            )
    finally:
        db.close()
    return out


def requeue_fleet_parked(path, *, ids=None, now=None):
    """Put parked fleet rows back in the queue with a fresh attempt budget and age.

    They keep their id, so they go out in their original order. Returns the
    ids requeued (unknown or not-parked ids are skipped). Requeued rows count
    toward max_rows again on the next enqueue.
    """
    now = time.time() if now is None else now
    db = _outbox_db(path, now)
    try:
        q, args = "SELECT id FROM fleet_outbox WHERE parked_at IS NOT NULL", []
        if ids is not None:
            q += f" AND id IN ({','.join('?' * len(ids))})" if ids else " AND 0"
            args += list(ids)
        got = [r[0] for r in db.execute(q + " ORDER BY id", args)]
        db.executemany(
            "UPDATE fleet_outbox SET parked_at=NULL, park_reason=NULL, attempts=0,"
            " created_at=? WHERE id=?",
            [(now, i) for i in got],
        )
        db.commit()
    finally:
        db.close()
    return got
