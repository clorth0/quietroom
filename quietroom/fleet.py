"""Fleet client side: node identity, heartbeats, store-and-forward upload."""

from __future__ import annotations
import json
import sqlite3
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


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
    }


def _post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - operator-configured central
        return r.status


class FleetClient:
    """HTTPS push client with a SQLite store-and-forward outbox."""

    def __init__(self, base_url, token=None, sender=None, outbox_path=":memory:"):
        self.base = base_url.rstrip("/")
        self.token = token
        self.sender = sender or _post
        self.db = sqlite3.connect(str(outbox_path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fleet_outbox("
            "id INTEGER PRIMARY KEY, path TEXT, payload TEXT)"
        )
        self.db.commit()

    def _send(self, path, payload):
        body = json.dumps(payload, sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            self.sender(f"{self.base}{path}", body, headers)
            return True
        except Exception:
            self.db.execute(
                "INSERT INTO fleet_outbox(path,payload) VALUES(?,?)", (path, body.decode())
            )
            self.db.commit()
            return False

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
                continue
            self.db.execute("DELETE FROM fleet_outbox WHERE id=?", (row_id,))
            sent += 1
        self.db.commit()
        return sent

    def _send_direct(self, path, payload):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.sender(f"{self.base}{path}", payload.encode(), headers)
