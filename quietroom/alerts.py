"""Alerting for monitoring nodes: rules, dedup/cooldown, webhook + email, outbox.

Findings become incidents here, not at the detector. An alert is keyed by
(node, segment, frequency cluster); repeats inside the cooldown update the
open alert instead of notifying again.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import smtplib
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class AlertRule:
    min_score: float = 80.0
    min_raw_score: float | None = None
    cooldown_s: float = 3600.0
    segment: str | None = None
    band: str | None = None

    def matches(self, finding, segment):
        if self.segment and self.segment != segment:
            return False
        if self.band and self.band != finding.get("band"):
            return False
        if finding.get("score", 0) < self.min_score:
            return False
        if self.min_raw_score is not None and (finding.get("raw_score") or 0) < self.min_raw_score:
            return False
        return True


class AlertEngine:
    def __init__(self, rules, cluster_hz=25000):
        self.rules = list(rules)
        self.cluster_hz = cluster_hz
        self._alerts = {}  # key -> alert dict

    def _key(self, event, f):
        return (
            event.get("node_id", ""),
            event.get("segment", ""),
            round(f["freq_hz"] / self.cluster_hz),
        )

    def process(self, event):
        now = float(event.get("timestamp", time.time()))
        out = []
        for f in event.get("findings", []):
            rule = next((r for r in self.rules if r.matches(f, event.get("segment", ""))), None)
            if rule is None:
                continue
            key = self._key(event, f)
            existing = self._alerts.get(key)
            if existing and now - existing["last_notified"] < rule.cooldown_s:
                existing["hits"] += 1
                existing["last_seen"] = now
                existing["score"] = max(existing["score"], f.get("score", 0))
                continue
            alert = {
                "key": key,
                "node_id": event.get("node_id"),
                "segment": event.get("segment"),
                "freq_hz": f["freq_hz"],
                "score": f.get("score"),
                "raw_score": f.get("raw_score"),
                "band": f.get("band"),
                "reasons": list(f.get("reasons", [])),
                "first_seen": now if not existing else existing["first_seen"],
                "last_seen": now,
                "last_notified": now,
                "hits": 1 if not existing else existing["hits"] + 1,
                "state": "new" if not existing else existing["state"],
            }
            self._alerts[key] = alert
            out.append(alert)
        return out

    def open_alerts(self):
        return list(self._alerts.values())

    def acknowledge(self, key):
        self._alerts[key]["state"] = "acknowledged"

    def resolve(self, key):
        self._alerts[key]["state"] = "resolved"


def _default_sender(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - operator-configured URL
        return r.status


class WebhookNotifier:
    """POST JSON with an HMAC-SHA256 signature header (X-Quietroom-Signature)."""

    def __init__(self, url, secret=None, sender=None):
        self.url = url
        self.secret = secret
        self.sender = sender or _default_sender

    def send(self, payload):
        body = json.dumps(payload, sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Quietroom-Signature"] = hmac.new(
                self.secret.encode(), body, hashlib.sha256
            ).hexdigest()
        return self.sender(self.url, body, headers)


class EmailNotifier:
    """Send an alert as a plain-text email over SMTP (stdlib smtplib).

    TLS is verified against the system trust store, or `ca_file` for a private
    relay. Note smtplib's own starttls() default context does *not* check the
    server certificate; we always pass a verifying context. Credentials are
    only ever sent after TLS is up: STARTTLS is required (not opportunistic)
    when `starttls` is set, and the caller refuses username-without-TLS.
    """

    def __init__(
        self,
        from_addr,
        to_addrs,
        host="localhost",
        port=25,
        smtp_factory=None,
        *,
        starttls=False,
        ssl=False,
        username="",
        password=None,
        ca_file="",
        timeout=10.0,
    ):
        self.from_addr = from_addr
        self.to_addrs = list(to_addrs)
        self.host = host
        self.port = port
        self.smtp_factory = smtp_factory
        self.starttls = starttls
        self.ssl = ssl
        self.username = username
        self.password = password
        self.ca_file = ca_file
        self.timeout = timeout

    def _tls_context(self):
        import ssl as _ssl

        return _ssl.create_default_context(cafile=self.ca_file or None)

    def _connect(self):
        if self.smtp_factory is not None:
            return self.smtp_factory(self.host, self.port)
        if self.ssl:
            return smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=self._tls_context()
            )
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def message(self, payload):
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        freq = payload.get("freq_hz")
        where = f"{freq / 1e6:.3f} MHz" if isinstance(freq, (int, float)) else ""
        score = payload.get("score")
        sc = f" score {score:.0f}" if isinstance(score, (int, float)) else ""
        msg["Subject"] = (
            f"[quietroom] alert {payload.get('node_id')} {payload.get('segment') or ''} "
            f"{where}{sc}"
        ).replace("  ", " ")
        msg.set_content(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return msg

    def send(self, payload):
        msg = self.message(payload)
        with self._connect() as s:
            if self.starttls and not self.ssl:
                s.ehlo()
                if not s.has_extn("starttls"):
                    raise smtplib.SMTPNotSupportedError("server does not offer STARTTLS")
                s.starttls(context=self._tls_context())
                s.ehlo()
            if self.username:
                s.login(self.username, self.password or "")
            s.sendmail(self.from_addr, self.to_addrs, msg.as_string())


_OUTBOX_COLUMNS = {
    "channel": "TEXT DEFAULT 'default'",
    "next_attempt_at": "REAL DEFAULT 0",
    "last_error": "TEXT DEFAULT ''",
}


class Outbox:
    """SQLite-backed delivery queue: at-least-once with retry on failure.

    One table, partitioned by `channel`, so webhook and email retry
    independently (a down SMTP relay never re-sends an already-delivered
    webhook). Delivery is FIFO; a failed head row backs off exponentially from
    `backoff_s` up to `max_backoff_s` and holds the channel until it is due,
    and with `stop_on_failure` a flush stops at the first failure, so a dead
    server costs one timeout per backoff period, not one per queued alert.
    Nothing is dropped: rows stay until delivered.
    """

    def __init__(self, path, channel="default", backoff_s=0.0, max_backoff_s=3600.0, clock=None):
        self.db = sqlite3.connect(str(path))
        self.channel = channel
        self.backoff_s = float(backoff_s)
        self.max_backoff_s = float(max_backoff_s)
        self.clock = clock or time.time
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS outbox("
            "id INTEGER PRIMARY KEY, payload TEXT, attempts INTEGER DEFAULT 0)"
        )
        have = {r[1] for r in self.db.execute("PRAGMA table_info(outbox)")}
        for col, decl in _OUTBOX_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {col} {decl}")
        self.db.commit()

    def add(self, payload):
        self.db.execute(
            "INSERT INTO outbox(payload,channel,next_attempt_at) VALUES(?,?,0)",
            (json.dumps(payload, sort_keys=True, default=str), self.channel),
        )
        self.db.commit()

    def pending(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM outbox WHERE channel=?", (self.channel,)
        ).fetchone()[0]

    def flush(self, send, stop_on_failure=True):
        now = self.clock()
        delivered = 0
        rows = self.db.execute(
            "SELECT id,payload,attempts,next_attempt_at FROM outbox WHERE channel=? ORDER BY id",
            (self.channel,),
        ).fetchall()
        # FIFO with head-of-line backoff: if the oldest alert is still backing
        # off, the channel is backing off (it is the relay that is down, not
        # one message), and alerts are never delivered out of order.
        if rows and rows[0][3] > now:
            return 0
        rows = [r[:3] for r in rows]
        for row_id, payload, attempts in rows:
            try:
                send(json.loads(payload))
            except Exception as e:
                delay = min(self.backoff_s * (2 ** int(attempts)), self.max_backoff_s)
                self.db.execute(
                    "UPDATE outbox SET attempts=attempts+1, next_attempt_at=?, last_error=? "
                    "WHERE id=?",
                    (now + delay, f"{type(e).__name__}: {e}"[:500], row_id),
                )
                if stop_on_failure:
                    break
                continue
            self.db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            delivered += 1
        self.db.commit()
        return delivered


class QueuedChannel:
    """A named alert sink: calling it enqueues; flush() delivers with retries."""

    def __init__(self, name, notifier_send, outbox):
        self.name = name
        self.send = notifier_send
        self.outbox = outbox
        self.notifier = getattr(notifier_send, "__self__", None)

    def __call__(self, alert):
        self.outbox.add(alert)

    def flush(self):
        return self.outbox.flush(self.send)

    def pending(self):
        return self.outbox.pending()
