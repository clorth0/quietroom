"""Alerting for monitoring nodes: rules, dedup/cooldown, webhook + email, outbox.

Findings become incidents here, not at the detector. An alert is keyed by
(node, segment, frequency cluster); repeats inside the cooldown update the
open alert instead of notifying again.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import math
import smtplib
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

from quietroom.sqlitedb import Schema, open_db


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
        key = (
            event.get("node_id", ""),
            event.get("segment", ""),
            round(f["freq_hz"] / self.cluster_hz),
        )
        kind = event.get("type", "sweep")
        # "on air now" and "the baseline drifted from its anchor here" are
        # different incidents; never let one's cooldown swallow the other.
        return key if kind == "sweep" else (*key, kind)

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
                "event_type": event.get("type", "sweep"),
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
    "created_at": "REAL",
    "last_attempt_at": "REAL DEFAULT 0",
    "parked_at": "REAL",
    "park_reason": "TEXT",
}

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_AGE_S = 7 * 86400.0
# Delivery attempts per flush (per tick, per channel), like the fleet outbox's
# FLUSH_BATCH: a long backlog drains over several ticks instead of holding one
# tick past the systemd watchdog.
ALERT_FLUSH_BATCH = 500
# ...and a wall-clock budget per flush: a relay that is up but slow (1 s a
# message) must not hold a tick for 500 s. Once spent, no new send starts; the
# one in flight finishes (at most one send timeout more) and the rest stay
# queued. 15 s: two alert channels and the fleet outbox, each a full budget plus
# one 10 s send in flight, still leave a tick well inside WatchdogSec=120.
ALERT_FLUSH_BUDGET_S = 15.0


def check_flush_budget(value, what="flush_budget_s"):
    """A positive, finite number of seconds; ValueError naming ``what`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number of seconds > 0, got {value!r}")
    if not (value > 0 and math.isfinite(value)):
        raise ValueError(f"{what} must be a number of seconds > 0, got {value!r}")
    return float(value)

ALERT_OUTBOX_SCHEMA = Schema(
    "alert outbox",
    statements=(
        "CREATE TABLE IF NOT EXISTS outbox("
        "id INTEGER PRIMARY KEY, payload TEXT, attempts INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS outbox_channels(channel TEXT PRIMARY KEY, last_ok_at REAL)",
    ),
    columns={"outbox": _OUTBOX_COLUMNS},
)


class Outbox:
    """SQLite-backed delivery queue: at-least-once with retry on failure.

    One table, partitioned by `channel`, so webhook and email retry
    independently (a down SMTP relay never re-sends an already-delivered
    webhook). Delivery is FIFO; a failed head row backs off exponentially from
    `backoff_s` up to `max_backoff_s` and holds the channel until it is due,
    and with `stop_on_failure` a flush stops at the first failure, so a dead
    server costs one timeout per backoff period, not one per queued alert.

    Dead letters. A row that has failed `max_attempts` times is a *suspect*:
    it no longer holds the channel (later rows go out past it) and is retried
    on its own backoff after them. A suspect is *parked* (kept, never retried,
    listed and requeued with ``quietroom-node --outbox``) only when it fails
    again after the channel delivered something else since its previous
    failure: proof that the message, not the relay, is the problem. An outage
    therefore never parks anything. Independently, a row older than
    `max_age_s` is parked as stale. 0 disables either limit.
    """

    def __init__(
        self,
        path,
        channel="default",
        backoff_s=0.0,
        max_backoff_s=3600.0,
        clock=None,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        max_age_s=DEFAULT_MAX_AGE_S,
        flush_batch=ALERT_FLUSH_BATCH,
        flush_budget_s=ALERT_FLUSH_BUDGET_S,
        monotonic=time.monotonic,
    ):
        if isinstance(flush_batch, bool) or not isinstance(flush_batch, int) or flush_batch < 1:
            raise ValueError(
                f"alert outbox flush_batch must be an integer >= 1, got {flush_batch!r}"
            )
        self.flush_batch = flush_batch
        self.flush_budget_s = check_flush_budget(flush_budget_s, "alert outbox flush_budget_s")
        self.monotonic = monotonic
        # Did the most recent flush stop because its time budget ran out?
        self.last_flush_budget_spent = False
        self.clock = clock or time.time
        self.db = _outbox_db(path, self.clock())
        self.channel = channel
        self.backoff_s = float(backoff_s)
        self.max_backoff_s = float(max_backoff_s)
        self.max_attempts = int(max_attempts)
        self.max_age_s = float(max_age_s)
        self.last_parked: list[dict] = []

    def close(self):
        self.db.close()

    def add(self, payload):
        self.db.execute(
            "INSERT INTO outbox(payload,channel,next_attempt_at,created_at) VALUES(?,?,0,?)",
            (json.dumps(payload, sort_keys=True, default=str), self.channel, self.clock()),
        )
        self.db.commit()

    def pending(self):
        """Rows still being delivered (parked rows excluded)."""
        return self.db.execute(
            "SELECT COUNT(*) FROM outbox WHERE channel=? AND parked_at IS NULL", (self.channel,)
        ).fetchone()[0]

    def parked(self):
        return self.db.execute(
            "SELECT COUNT(*) FROM outbox WHERE channel=? AND parked_at IS NOT NULL",
            (self.channel,),
        ).fetchone()[0]

    def _park(self, row_id, now, reason, attempts):
        self.db.execute(
            "UPDATE outbox SET parked_at=?, park_reason=? WHERE id=?", (now, reason, row_id)
        )
        self.last_parked.append(
            {"id": row_id, "channel": self.channel, "reason": reason, "attempts": attempts}
        )

    def _last_ok(self):
        r = self.db.execute(
            "SELECT last_ok_at FROM outbox_channels WHERE channel=?", (self.channel,)
        ).fetchone()
        return r[0] if r else None

    def _attempt(self, send, row, now):
        """Try one row; True if delivered. Failure backs it off (or parks a poison)."""
        row_id, payload, attempts, _, last_attempt = row
        try:
            send(json.loads(payload))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            delay = min(self.backoff_s * (2 ** int(attempts)), self.max_backoff_s)
            self.db.execute(
                "UPDATE outbox SET attempts=attempts+1, next_attempt_at=?, last_error=?, "
                "last_attempt_at=? WHERE id=?",
                (now + delay, err, now, row_id),
            )
            last_ok = self._last_ok()
            if self._suspect(attempts) and last_ok is not None and last_ok > last_attempt:
                self._park(
                    row_id, now,
                    f"poison: failed {int(attempts) + 1} times, still failing after the "
                    "channel delivered other alerts",
                    int(attempts) + 1,
                )
            return False
        self.db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
        self.db.execute(
            "INSERT INTO outbox_channels(channel,last_ok_at) VALUES(?,?) "
            "ON CONFLICT(channel) DO UPDATE SET last_ok_at=excluded.last_ok_at",
            (self.channel, now),
        )
        return True

    def _suspect(self, attempts):
        return self.max_attempts > 0 and int(attempts) >= self.max_attempts

    def flush(self, send, stop_on_failure=True, *, budget_s=None, should_stop=None):
        """Deliver due alerts in order; returns how many went out.

        Stops starting sends after ``flush_batch`` attempts, once ``budget_s``
        seconds (default ``flush_budget_s``) have passed, or once
        ``should_stop()`` is true. Whatever is left stays queued.
        """
        budget = self.flush_budget_s if budget_s is None else float(budget_s)
        deadline = self.monotonic() + budget
        self.last_flush_budget_spent = False

        def out_of_time():
            if self.monotonic() >= deadline:
                self.last_flush_budget_spent = True
                return True
            return should_stop is not None and should_stop()

        now = self.clock()
        self.last_parked = []
        if self.max_age_s > 0:
            for row_id, attempts in self.db.execute(
                "SELECT id,attempts FROM outbox WHERE channel=? AND parked_at IS NULL"
                " AND created_at<=? ORDER BY id",
                (self.channel, now - self.max_age_s),
            ).fetchall():
                self._park(row_id, now, f"max_age: undelivered for {self.max_age_s:g} s",
                           int(attempts))
        rows = self.db.execute(
            "SELECT id,payload,attempts,next_attempt_at,last_attempt_at FROM outbox"
            " WHERE channel=? AND parked_at IS NULL ORDER BY id",
            (self.channel,),
        ).fetchall()
        delivered, failed, tried = 0, False, 0
        suspects = [r for r in rows if self._suspect(r[2])]
        # FIFO with head-of-line backoff: if the oldest alert is still backing
        # off, the channel is backing off (it is the relay that is down, not
        # one message), and alerts are never delivered out of order. Suspects
        # are skipped here: they have used up their right to hold the channel.
        for row in rows:
            if self._suspect(row[2]):
                continue
            if row[3] > now:
                break
            if tried >= self.flush_batch or out_of_time():
                break
            tried += 1
            if self._attempt(send, row, now):
                delivered += 1
                continue
            failed = True
            if stop_on_failure:
                break
        # Then the suspects, oldest first, each on its own backoff; a failure
        # stops the pass, so an outage still costs one attempt per flush.
        if not (failed and stop_on_failure):
            for row in suspects:
                if row[3] > now:
                    continue
                if tried >= self.flush_batch or out_of_time():
                    break
                tried += 1
                if self._attempt(send, row, now):
                    delivered += 1
                    continue
                if stop_on_failure:
                    break
        self.db.commit()
        return delivered


def _outbox_db(path, now):
    """Open (creating or upgrading in place) an alert outbox file."""

    def backfill(db):
        # rows queued before ages were tracked count from the upgrade, not from 1970
        db.execute("UPDATE outbox SET created_at=? WHERE created_at IS NULL", (now,))

    return open_db(path, ALERT_OUTBOX_SCHEMA, upgrade=backfill)


def _summary(p):
    if not isinstance(p, dict):
        return ""
    parts = [str(p[k]) for k in ("node_id", "segment") if p.get(k)]
    if isinstance(p.get("freq_hz"), (int, float)):
        parts.append(f"{p['freq_hz'] / 1e6:.3f} MHz")
    if isinstance(p.get("score"), (int, float)):
        parts.append(f"score {p['score']:.0f}")
    return " ".join(parts)


def outbox_rows(path, *, channel=None, parked=None):
    """Rows of an alert outbox file for inspection (``quietroom-node --outbox list``).

    parked: None = all, True = parked only, False = still being delivered.
    """
    db = _outbox_db(path, time.time())
    q = (
        "SELECT id,channel,attempts,created_at,last_attempt_at,next_attempt_at,last_error,"
        "parked_at,park_reason,payload FROM outbox WHERE 1=1"
    )
    args: list = []
    if channel is not None:
        q += " AND channel=?"
        args.append(channel)
    if parked is not None:
        q += " AND parked_at IS NOT NULL" if parked else " AND parked_at IS NULL"
    out = []
    for r in db.execute(q + " ORDER BY id", args):
        try:
            payload = json.loads(r[9])
        except (TypeError, ValueError):
            payload = r[9]
        out.append(
            {
                "id": r[0], "channel": r[1], "attempts": r[2], "created_at": r[3],
                "last_attempt_at": r[4] or None, "next_attempt_at": r[5],
                "last_error": r[6] or "", "parked_at": r[7], "park_reason": r[8],
                "summary": _summary(payload), "payload": payload,
            }
        )
    db.close()
    return out


def requeue_parked(path, *, ids=None, channel=None, now=None):
    """Put parked rows back in line with a fresh attempt budget and age.

    They keep their id, so they go out in their original order. Returns the
    ids requeued (ids that are unknown or not parked are skipped).
    """
    now = time.time() if now is None else now
    db = _outbox_db(path, now)
    q, args = "SELECT id FROM outbox WHERE parked_at IS NOT NULL", []
    if channel is not None:
        q += " AND channel=?"
        args.append(channel)
    if ids is not None:
        q += f" AND id IN ({','.join('?' * len(ids))})" if ids else " AND 0"
        args += list(ids)
    got = [r[0] for r in db.execute(q + " ORDER BY id", args)]
    db.executemany(
        "UPDATE outbox SET parked_at=NULL, park_reason=NULL, attempts=0, next_attempt_at=0,"
        " last_attempt_at=0, created_at=? WHERE id=?",
        [(now, i) for i in got],
    )
    db.commit()
    db.close()
    return got


class QueuedChannel:
    """A named alert sink: calling it enqueues; flush() delivers with retries."""

    def __init__(self, name, notifier_send, outbox):
        self.name = name
        self.send = notifier_send
        self.outbox = outbox
        self.notifier = getattr(notifier_send, "__self__", None)

    def __call__(self, alert):
        self.outbox.add(alert)

    def flush(self, *, budget_s=None, should_stop=None):
        return self.outbox.flush(self.send, budget_s=budget_s, should_stop=should_stop)

    def close(self):
        self.outbox.close()

    def pending(self):
        return self.outbox.pending()

    def parked(self):
        return self.outbox.parked()

    @property
    def last_parked(self):
        """Rows the most recent flush parked: id, channel, reason, attempts (no payload)."""
        return list(self.outbox.last_parked)
