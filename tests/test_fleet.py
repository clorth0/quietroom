from quietroom.fleet import NodeIdentity, heartbeat_payload, FleetClient
from quietroom.central import create_central_app


def test_identity_persists(tmp_path):
    p = tmp_path / "id.json"
    a = NodeIdentity.load_or_create(p, site="hq", floor="3", room="lab")
    b = NodeIdentity.load_or_create(p)
    assert a.node_id == b.node_id and a.site == "hq"


def test_heartbeat_payload_fields():
    i = NodeIdentity("n1", "hq", "3", "lab", "0.3.0", "cfg1")
    h = heartbeat_payload(i, health={"baselines": ["uhf"], "last_run": {"uhf": 10.0}}, now=100.0)
    assert h["node_id"] == "n1" and h["segments"] == {"uhf": 10.0} and h["version"] == "0.3.0"


def test_central_ingest_heartbeat_and_events():
    app = create_central_app(token="tok1234567890123")
    c = app.test_client()
    assert (
        c.post(
            "/api/v1/heartbeat", json={"node_id": "n1"}, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 403
    )
    h = {"Authorization": "Bearer tok1234567890123"}
    assert (
        c.post(
            "/api/v1/heartbeat",
            json=heartbeat_payload(NodeIdentity("n1", "hq", "3", "lab"), {}, 100.0),
            headers=h,
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/api/v1/events",
            json={"node_id": "n1", "events": [{"segment": "uhf", "findings": []}]},
            headers=h,
        ).status_code
        == 200
    )
    nodes = c.get("/api/v1/nodes", headers=h).get_json()["nodes"]
    assert nodes[0]["node_id"] == "n1"
    ev = c.get("/api/v1/events?node_id=n1", headers=h).get_json()["events"]
    assert ev[0]["segment"] == "uhf"


def test_client_queues_on_failure(tmp_path):
    def down(url, body, headers):
        raise RuntimeError("offline")

    cl = FleetClient("https://central.test", token="t", sender=down, outbox_path=tmp_path / "o.db")
    assert cl.send_heartbeat({"node_id": "n1"}) is False and cl.pending() == 1
    sent = []
    cl.sender = lambda u, b, h: sent.append(b)
    assert cl.flush() == 1 and cl.pending() == 0
