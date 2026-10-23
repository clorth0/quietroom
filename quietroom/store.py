"""SQLite persistence for monitoring nodes: baselines, sweeps, findings.

Spectra are stored as compressed NumPy blobs (frequency grid implicit in the
row's start/stop/bin metadata is *not* assumed — the full freq array is saved
in the blob). Retention is explicit via prune(); nothing is deleted silently.
"""

from __future__ import annotations
import io
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any
import numpy as np
from quietroom.engine.spectrum import Baseline

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
 node_id TEXT PRIMARY KEY, site TEXT, floor TEXT, room TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS baselines(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, version INTEGER,
 created_at REAL, sweep_count INTEGER, blob BLOB, UNIQUE(node_id,segment,version));
CREATE TABLE IF NOT EXISTS sweeps(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, timestamp REAL, blob BLOB);
CREATE TABLE IF NOT EXISTS findings(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, timestamp REAL,
 freq_hz REAL, score REAL, raw_score REAL, band TEXT, reasons TEXT);
CREATE INDEX IF NOT EXISTS idx_findings_node ON findings(node_id,timestamp);
CREATE INDEX IF NOT EXISTS idx_sweeps_node ON sweeps(node_id,timestamp);
CREATE TABLE IF NOT EXISTS baseline_anchors(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, fingerprint TEXT, version INTEGER,
 set_at REAL, reason TEXT, detail TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS control_requests(
 id INTEGER PRIMARY KEY, node_id TEXT, segment TEXT, command TEXT, args TEXT DEFAULT '{}',
 requested_at REAL, status TEXT DEFAULT 'pending', result TEXT DEFAULT '{}', done_at REAL);
CREATE INDEX IF NOT EXISTS idx_control_pending ON control_requests(node_id,status);
"""

CONTROL_COMMANDS = ("rebaseline", "ack", "promote-anchor")

# Columns added after the first schema shipped; applied with ALTER TABLE so an
# existing node database upgrades in place. Pre-policy rows read back with an
# empty fingerprint, which the re-baseline policy treats as "unknown front end".
_BASELINE_COLUMNS = {
    "reason": "TEXT DEFAULT ''",
    "fingerprint": "TEXT DEFAULT ''",
    "captured_at": "REAL",
    "detail": "TEXT DEFAULT '{}'",
}


@dataclass(frozen=True)
class BaselineMeta:
    """Why, when and under which front end a baseline version was captured."""

    version: int
    captured_at: float
    reason: str
    fingerprint: str
    detail: dict[str, Any] = field(default_factory=dict)


def _blob(ps):
    buf = io.BytesIO()
    np.savez_compressed(buf, freqs=ps.freqs_hz, power=ps.power_dbm)
    return buf.getvalue()


def _unblob(b):
    d = np.load(io.BytesIO(b))
    return d["freqs"], d["power"]


# How long an open waits on another connection (the daemon, or a CLI command
# racing it) before giving up. Applies to every statement on the connection.
BUSY_TIMEOUT_S = 30.0


def _is_lock_error(e: sqlite3.OperationalError) -> bool:
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


class Store:
    """One node's SQLite database. Safe to open from several processes at once.

    The daemon and ``quietroom-node --rebaseline/--ack/--promote-anchor`` share
    this file, so opening it (switch to WAL, create tables, upgrade an older
    schema) is serialized: the WAL switch is retried while another opener holds
    the lock, and schema setup runs in one ``BEGIN IMMEDIATE`` transaction so
    exactly one opener applies a column migration and the rest see it done.
    """

    def __init__(self, path, busy_timeout_s: float = BUSY_TIMEOUT_S):
        self.db = sqlite3.connect(str(path), timeout=busy_timeout_s)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
            self._enable_wal(busy_timeout_s)
            self._ensure_schema()
        except BaseException:
            self.db.close()
            raise

    def _enable_wal(self, busy_timeout_s: float) -> None:
        # Switching journal mode needs an exclusive lock and SQLite can report
        # SQLITE_BUSY here without consulting the busy handler, so retry it.
        deadline = time.monotonic() + busy_timeout_s
        delay = 0.005
        while True:
            try:
                self.db.execute("PRAGMA journal_mode=WAL").fetchone()
                return
            except sqlite3.OperationalError as e:
                if not _is_lock_error(e) or time.monotonic() >= deadline:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)

    def _ensure_schema(self) -> None:
        # executescript() would COMMIT first, so run the statements one by one
        # inside a single write transaction taken up front.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.db.execute(stmt)
            have = {r[1] for r in self.db.execute("PRAGMA table_info(baselines)")}
            for col, decl in _BASELINE_COLUMNS.items():
                if col in have:
                    continue
                try:
                    self.db.execute(f"ALTER TABLE baselines ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError as e:
                    # Harmless: someone else added it (e.g. a process that
                    # opened the file without this lock).
                    if "duplicate column" not in str(e).lower():
                        raise
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def upsert_node(self, node_id, site="", floor="", room=""):
        self.db.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET site=excluded.site,"
            "floor=excluded.floor,room=excluded.room,updated_at=excluded.updated_at",
            (node_id, site, floor, room, time.time()),
        )
        self.db.commit()

    def save_baseline(
        self,
        node_id,
        segment,
        baseline,
        *,
        reason="",
        fingerprint="",
        captured_at=None,
        detail=None,
    ):
        """Append a new baseline version; never overwrites an older one."""
        row = self.db.execute(
            "SELECT COALESCE(MAX(version),0)+1 v FROM baselines WHERE node_id=? AND segment=?",
            (node_id, segment),
        ).fetchone()
        v = int(row["v"])
        # store mean as spectrum blob plus std in a sidecar blob packed together
        buf = io.BytesIO()
        np.savez_compressed(
            buf, freqs=baseline.freqs_hz, power=baseline.mean_dbm, std=baseline.std_dbm
        )
        self.db.execute(
            "INSERT INTO baselines (node_id,segment,version,created_at,sweep_count,blob,"
            "reason,fingerprint,captured_at,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                node_id,
                segment,
                v,
                baseline.created_at,
                baseline.sweep_count,
                buf.getvalue(),
                str(reason),
                fingerprint,
                time.time() if captured_at is None else captured_at,
                json.dumps(detail or {}, sort_keys=True, default=str),
            ),
        )
        self.db.commit()
        return v

    def load_baseline(self, node_id, segment, version=None):
        q = (
            "SELECT * FROM baselines WHERE node_id=? AND segment=?"
            + ("" if version is None else " AND version=?")
            + " ORDER BY version DESC LIMIT 1"
        )
        args = (node_id, segment) if version is None else (node_id, segment, version)
        r = self.db.execute(q, args).fetchone()
        if r is None:
            return None
        d = np.load(io.BytesIO(r["blob"]))
        return Baseline(d["freqs"], d["power"], d["std"], r["sweep_count"], r["created_at"])

    def baseline_meta(self, node_id, segment, version=None):
        """Why/when/under which front end a baseline version was captured."""
        q = (
            "SELECT version,created_at,captured_at,reason,fingerprint,detail FROM baselines"
            " WHERE node_id=? AND segment=?"
            + ("" if version is None else " AND version=?")
            + " ORDER BY version DESC LIMIT 1"
        )
        args = (node_id, segment) if version is None else (node_id, segment, version)
        r = self.db.execute(q, args).fetchone()
        if r is None:
            return None
        captured = r["captured_at"] if r["captured_at"] is not None else r["created_at"]
        return BaselineMeta(
            version=int(r["version"]),
            captured_at=float(captured),
            reason=r["reason"] or "",
            fingerprint=r["fingerprint"] or "",
            detail=json.loads(r["detail"] or "{}"),
        )

    def last_incident_at(self, node_id, segment, min_score):
        """Timestamp of the newest finding scoring >= min_score, or None."""
        r = self.db.execute(
            "SELECT MAX(timestamp) t FROM findings WHERE node_id=? AND segment=? AND score>=?",
            (node_id, segment, min_score),
        ).fetchone()
        return None if r["t"] is None else float(r["t"])

    def incident_hits(self, node_id, segment, min_score, since):
        """(timestamp, freq_hz) of every finding scoring >= min_score at or after `since`."""
        return [
            (float(r["timestamp"]), None if r["freq_hz"] is None else float(r["freq_hz"]))
            for r in self.db.execute(
                "SELECT timestamp,freq_hz FROM findings WHERE node_id=? AND segment=?"
                " AND score>=? AND timestamp>=? ORDER BY timestamp",
                (node_id, segment, min_score, since),
            )
        ]

    # --- anchor (long-lived reference) baselines ----------------------------
    # Append-only: the newest row per (node, segment, front-end fingerprint) is
    # the anchor; older rows are the audit trail of promotions.

    def set_anchor(self, node_id, segment, fingerprint, version, *, set_at, reason, detail=None):
        self.db.execute(
            "INSERT INTO baseline_anchors(node_id,segment,fingerprint,version,set_at,reason,detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (node_id, segment, fingerprint, int(version), set_at, reason,
             json.dumps(detail or {}, sort_keys=True, default=str)),
        )
        self.db.commit()

    def anchor(self, node_id, segment, fingerprint):
        r = self.db.execute(
            "SELECT version,set_at,reason,detail FROM baseline_anchors"
            " WHERE node_id=? AND segment=? AND fingerprint=? ORDER BY id DESC LIMIT 1",
            (node_id, segment, fingerprint),
        ).fetchone()
        if r is None:
            return None
        return {
            "version": int(r["version"]),
            "set_at": float(r["set_at"]),
            "reason": r["reason"],
            "detail": json.loads(r["detail"] or "{}"),
        }

    def earliest_version(self, node_id, segment, fingerprint):
        """Oldest stored baseline version captured under this front end, or None."""
        r = self.db.execute(
            "SELECT MIN(version) v FROM baselines WHERE node_id=? AND segment=? AND fingerprint=?",
            (node_id, segment, fingerprint),
        ).fetchone()
        return None if r["v"] is None else int(r["v"])

    # --- operator control requests ------------------------------------------
    # The running node owns the radio and its in-memory state, so operator
    # commands (quietroom-node --rebaseline/--ack/--promote-anchor) are queued
    # here and applied by the daemon on its next tick. Rows are kept as the
    # audit trail.

    def request_control(self, node_id, segment, command, args=None, *, now=None):
        if command not in CONTROL_COMMANDS:
            raise ValueError(f"unknown control command {command!r}")
        cur = self.db.execute(
            "INSERT INTO control_requests(node_id,segment,command,args,requested_at)"
            " VALUES(?,?,?,?,?)",
            (node_id, segment, command, json.dumps(args or {}, sort_keys=True),
             time.time() if now is None else now),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def _control_row(self, r):
        return {
            "id": int(r["id"]),
            "node_id": r["node_id"],
            "segment": r["segment"],
            "command": r["command"],
            "args": json.loads(r["args"] or "{}"),
            "requested_at": float(r["requested_at"]),
            "status": r["status"],
            "result": json.loads(r["result"] or "{}"),
            "done_at": None if r["done_at"] is None else float(r["done_at"]),
        }

    def pending_controls(self, node_id):
        return [
            self._control_row(r)
            for r in self.db.execute(
                "SELECT * FROM control_requests WHERE node_id=? AND status='pending' ORDER BY id",
                (node_id,),
            )
        ]

    def control_request(self, request_id):
        r = self.db.execute(
            "SELECT * FROM control_requests WHERE id=?", (request_id,)
        ).fetchone()
        return None if r is None else self._control_row(r)

    def finish_control(self, request_id, status, result, *, now=None):
        self.db.execute(
            "UPDATE control_requests SET status=?, result=?, done_at=? WHERE id=?",
            (status, json.dumps(result, sort_keys=True, default=str),
             time.time() if now is None else now, request_id),
        )
        self.db.commit()

    def applied_acks(self, node_id, segment, since):
        """(requested_at, freq_hz or None) releases the node applied, requested >= since.

        A done ``ack`` releases its ``freq_hz`` (None = every emitter); a done
        ``promote-anchor`` releases each emitter it accepted (``accepted_hz``).
        """
        out = []
        for r in self.db.execute(
            "SELECT requested_at,command,args,result FROM control_requests WHERE node_id=?"
            " AND segment=? AND command IN ('ack','promote-anchor') AND status='done'"
            " AND requested_at>=? ORDER BY requested_at",
            (node_id, segment, since),
        ):
            at = float(r["requested_at"])
            if r["command"] == "ack":
                f = json.loads(r["args"] or "{}").get("freq_hz")
                out.append((at, None if f is None else float(f)))
            else:
                out += [(at, float(f)) for f in json.loads(r["result"] or "{}").get(
                    "accepted_hz", [])]
        return out

    def baseline_versions(self, node_id, segment):
        return [
            r["version"]
            for r in self.db.execute(
                "SELECT version FROM baselines WHERE node_id=? AND segment=? ORDER BY version",
                (node_id, segment),
            )
        ]

    def record_sweep(self, node_id, segment, spectrum, findings=(), timestamp=None):
        """timestamp: wall-clock time of the sweep (defaults to spectrum.timestamp)."""
        ts = spectrum.timestamp if timestamp is None else timestamp
        self.db.execute(
            "INSERT INTO sweeps(node_id,segment,timestamp,blob) VALUES(?,?,?,?)",
            (node_id, segment, ts, _blob(spectrum)),
        )
        for f in findings:
            self.db.execute(
                "INSERT INTO findings "
                "(node_id,segment,timestamp,freq_hz,score,raw_score,band,reasons) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    node_id,
                    segment,
                    ts,
                    f.get("freq_hz"),
                    f.get("score"),
                    f.get("raw_score"),
                    f.get("band", ""),
                    json.dumps(f.get("reasons", [])),
                ),
            )
        self.db.commit()

    def findings(self, node_id, since=None):
        q = "SELECT * FROM findings WHERE node_id=?"
        a = [node_id]
        if since is not None:
            q += " AND timestamp>=?"
            a.append(since)
        return [dict(r) for r in self.db.execute(q + " ORDER BY timestamp", a)]

    def sweep_count(self, node_id):
        return self.db.execute(
            "SELECT COUNT(*) c FROM sweeps WHERE node_id=?", (node_id,)
        ).fetchone()["c"]

    def prune(self, now=None, max_age_s=None, max_sweeps=None):
        now = time.time() if now is None else now
        deleted = 0
        if max_age_s is not None:
            cur = self.db.execute("DELETE FROM sweeps WHERE timestamp<?", (now - max_age_s,))
            deleted += cur.rowcount
            self.db.execute("DELETE FROM findings WHERE timestamp<?", (now - max_age_s,))
        if max_sweeps is not None:
            cur = self.db.execute(
                "DELETE FROM sweeps WHERE id NOT IN "
                "(SELECT id FROM sweeps ORDER BY id DESC LIMIT ?)",
                (max_sweeps,),
            )
            deleted += cur.rowcount
        self.db.commit()
        return deleted

    def export_json(self, node_id):
        return {
            "node_id": node_id,
            "findings": self.findings(node_id),
            "sweeps": self.sweep_count(node_id),
        }
