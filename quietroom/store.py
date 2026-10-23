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
"""

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


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(baselines)")}
        for col, decl in _BASELINE_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE baselines ADD COLUMN {col} {decl}")
        self.db.commit()

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
