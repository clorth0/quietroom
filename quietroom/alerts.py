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
    def __init__(self, from_addr, to_addrs, host="localhost", port=25, smtp_factory=None):
        self.from_addr = from_addr
        self.to_addrs = list(to_addrs)
        self.host = host
        self.port = port
        self.smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, payload):
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg["Subject"] = f"[quietroom] alert {payload.get('node_id')} {payload.get('freq_hz', '')}"
        msg.set_content(json.dumps(payload, indent=2, sort_keys=True))
        with self.smtp_factory(self.host, self.port) as s:
            s.sendmail(self.from_addr, self.to_addrs, msg.as_string())


class Outbox:
    """SQLite-backed delivery queue: at-least-once with retry on failure."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS outbox("
            "id INTEGER PRIMARY KEY, payload TEXT, attempts INTEGER DEFAULT 0)"
        )
        self.db.commit()

    def add(self, payload):
        self.db.execute(
            "INSERT INTO outbox(payload) VALUES(?)", (json.dumps(payload, sort_keys=True),)
        )
        self.db.commit()

    def pending(self):
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def flush(self, send):
        delivered = 0
        for row_id, payload in self.db.execute(
            "SELECT id,payload FROM outbox ORDER BY id"
        ).fetchall():
            try:
                send(json.loads(payload))
            except Exception:
                self.db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (row_id,))
                continue
            self.db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            delivered += 1
        self.db.commit()
        return delivered
