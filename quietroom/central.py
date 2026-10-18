"""Minimal central aggregation service: node registry, heartbeats, event ingest.

This is the fleet counterpart to quietroom.fleet. Auth here is a shared
bearer token over (assumed) TLS; per-node mTLS identity is item 6. State is
in-memory/SQLite-free by design for this MVP — the item-2 Store schema is the
persistence path when these PRs integrate.
"""

from __future__ import annotations
import hmac
import time
from flask import Flask, abort, request


def create_central_app(token=None, stale_after_s=180):
    app = Flask(__name__)
    nodes = {}  # node_id -> heartbeat dict
    events = []  # ingested event batches' events
    app.central_nodes = nodes
    app.central_events = events

    @app.before_request
    def _auth():
        if token is None:
            return
        given = request.headers.get("Authorization", "")
        if not hmac.compare_digest(given, f"Bearer {token}"):
            abort(403)

    @app.post("/api/v1/heartbeat")
    def heartbeat():
        d = request.get_json(force=True)
        nodes[d["node_id"]] = d
        return {"ok": True, "nodes": len(nodes)}

    @app.post("/api/v1/events")
    def ingest():
        d = request.get_json(force=True)
        for e in d.get("events", []):
            events.append({"node_id": d.get("node_id"), **e})
        return {"ok": True, "stored": len(events)}

    @app.get("/api/v1/nodes")
    def list_nodes():
        now = time.time()
        out = [{**n, "stale": now - n.get("timestamp", 0) > stale_after_s} for n in nodes.values()]
        return {"nodes": out}

    @app.get("/api/v1/events")
    def list_events():
        node = request.args.get("node_id")
        return {"events": [e for e in events if node is None or e.get("node_id") == node]}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "nodes": len(nodes), "events": len(events)}

    return app
