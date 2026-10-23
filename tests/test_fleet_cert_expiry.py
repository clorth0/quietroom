"""Client-certificate expiry: the node reports it, central records it from TLS and warns."""

from __future__ import annotations

import contextlib
import threading
import time

import pytest

pytest.importorskip("cryptography")

from _pki import make_ca, node_cert, server_cert  # noqa: E402

from quietroom.central import (  # noqa: E402
    PEER_CERT_ENVIRON,
    create_central_app,
    make_central_server,
    server_tls_context,
)
from quietroom.config import load_config  # noqa: E402
from quietroom.fleet import (  # noqa: E402
    FleetClient,
    NodeIdentity,
    cert_expiry,
    cert_file_not_after,
    client_tls_context,
    heartbeat_payload,
)
from quietroom.node.cli import build_fleet  # noqa: E402

U = "6f1c2a52-6a0e-4c35-9a53-0b8a3a6f5e01"
DAY = 86400.0
READ = "read-token-0123456789"


@contextlib.contextmanager
def serving(app, ctx):
    srv = make_central_server(app, "127.0.0.1", 0, ctx)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"https://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        t.join(5)


def test_cert_expiry_status():
    s = cert_expiry(1_000_000 + 10 * DAY, now=1_000_000, warn_s=30 * DAY)
    assert s == {
        "not_after": "1970-01-22T13:46:40Z",
        "expires_in_s": int(10 * DAY),
        "expiring": True,
        "expired": False,
    }
    assert cert_expiry(1_000_000 + 40 * DAY, now=1_000_000, warn_s=30 * DAY)["expiring"] is False
    gone = cert_expiry(1_000_000 - 1, now=1_000_000, warn_s=30 * DAY)
    assert gone["expired"] is True and gone["expiring"] is True and gone["expires_in_s"] == -1
    assert cert_expiry(1_000_000 + DAY, now=1_000_000, warn_s=0)["expiring"] is False


def test_cert_file_not_after_matches_the_issued_validity(tmp_path):
    ca = make_ca(tmp_path, "node ca")
    c, _ = node_cert(ca, tmp_path, U, days=10)
    assert abs(cert_file_not_after(c) - (time.time() + 10 * DAY)) < 120


def _node_cfg(tmp_path, days, warn=None):
    ca = make_ca(tmp_path, "node ca")
    sca = make_ca(tmp_path, "central ca")
    c, k = node_cert(ca, tmp_path, U, days=days)
    ident = tmp_path / "identity.json"
    ident.write_text(f'{{"node_id": "{U}"}}')
    p = tmp_path / "node.toml"
    p.write_text(
        '[node]\nid = "n1"\n'
        '[[segments]]\nname = "uhf"\nstart_hz = 300000000\nstop_hz = 320000000\nbin_hz = 25000\n'
        '[fleet]\nbase_url = "https://central.test:8443"\n'
        f'cert_file = "{c}"\nkey_file = "{k}"\nca_file = "{sca.path}"\n'
        f'identity_path = "{ident}"\n' + (f"cert_warn_days = {warn}\n" if warn is not None else "")
    )
    return load_config(p)


def test_cert_warn_days_config(tmp_path):
    assert _node_cfg(tmp_path, 365).fleet.cert_warn_days == 30
    assert _node_cfg(tmp_path, 365, warn=7).fleet.cert_warn_days == 7
    assert _node_cfg(tmp_path, 365, warn=0).fleet.cert_warn_days == 0  # 0 = never warn


@pytest.mark.parametrize("bad", ["-1", "true", '"30"'])
def test_cert_warn_days_is_validated(tmp_path, bad):
    with pytest.raises(ValueError, match="cert_warn_days"):
        _node_cfg(tmp_path, 365, warn=bad)


def test_build_fleet_warns_at_startup_when_the_cert_is_near_expiry(tmp_path):
    client, warnings = build_fleet(_node_cfg(tmp_path, 10), {})
    assert any("expires in 9 days" in w or "expires in 10 days" in w for w in warnings)
    status = client.cert_status()
    assert status["expiring"] is True and 9 * DAY < status["expires_in_s"] <= 10 * DAY


def test_build_fleet_is_quiet_for_a_fresh_cert(tmp_path):
    client, warnings = build_fleet(_node_cfg(tmp_path, 365), {})
    assert not any("expire" in w for w in warnings)
    assert client.cert_status()["expiring"] is False


def test_node_health_and_heartbeat_report_the_cert_and_warn_dynamically(tmp_path):
    from quietroom.node.service import MonitorService

    cfg = _node_cfg(tmp_path, 365)
    client, warnings = build_fleet(cfg, {})
    svc = MonitorService(cfg, None, fleet=client, fleet_warnings=warnings)
    h = svc.health()
    assert h["fleet_cert"]["expiring"] is False and h["fleet_cert"]["not_after"].endswith("Z")
    # the daemon runs for months: the warning must appear without a restart
    client.cert_not_after = time.time() + 3 * DAY
    h = svc.health()
    assert h["fleet_cert"]["expiring"] is True
    assert any("client certificate expires in" in w for w in h["fleet_warnings"])
    hb = heartbeat_payload(NodeIdentity("n1"), h, 100.0)
    assert hb["fleet_cert"] == h["fleet_cert"]


def _peer(u, not_after):
    return {
        "subject": ((("commonName", u),),),
        "subjectAltName": (("URI", f"urn:uuid:{u}"),),
        "notAfter": not_after,
    }


def test_central_records_expiry_from_the_tls_cert_not_the_body(capsys):
    app = create_central_app(require_client_cert=True, read_token=READ, cert_warn_days=30)
    c = app.test_client()
    soon = time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(time.time() + 5 * DAY))
    body = {
        **heartbeat_payload(NodeIdentity("n1"), {}, time.time()),
        "node_uuid": U,
        "cert": {"expiring": False, "not_after": "2099-01-01T00:00:00Z"},  # forged claim
    }
    env = {PEER_CERT_ENVIRON: _peer(U, soon)}
    for _ in range(3):
        assert c.post("/api/v1/heartbeat", json=body, environ_base=env).status_code == 200
    (n,) = c.get("/api/v1/nodes", headers={"Authorization": f"Bearer {READ}"}).get_json()["nodes"]
    assert n["cert"]["expiring"] is True and 4 * DAY < n["cert"]["expires_in_s"] <= 5 * DAY
    assert c.get("/healthz").get_json()["certs_expiring"] == 1
    err = capsys.readouterr().err
    assert err.count(f"node {U}") == 1 and "client certificate expires in" in err  # warn once


def test_legacy_nodes_have_no_cert_and_cannot_claim_one():
    app = create_central_app("legacy-token-0123456789", require_client_cert=True, read_token=READ)
    c = app.test_client()
    body = {**heartbeat_payload(NodeIdentity("n1"), {}, time.time()), "cert": {"expired": True}}
    auth = {"Authorization": "Bearer legacy-token-0123456789"}
    assert c.post("/api/v1/heartbeat", json=body, headers=auth).status_code == 200
    (n,) = c.get("/api/v1/nodes", headers={"Authorization": f"Bearer {READ}"}).get_json()["nodes"]
    assert n["cert"] is None
    assert c.get("/healthz").get_json()["certs_expiring"] == 0


def test_real_tls_handshake_carries_the_expiry_to_central(tmp_path):
    nca = make_ca(tmp_path, "node ca")
    sca = make_ca(tmp_path, "central ca")
    sc, sk = server_cert(sca, tmp_path)
    nc, nk = node_cert(nca, tmp_path, U, days=12)
    app = create_central_app(require_client_cert=True, read_token=READ)
    with serving(app, server_tls_context(sc, sk, nca.path)) as base:
        cl = FleetClient(base, ssl_context=client_tls_context(nc, nk, sca.path), node_uuid=U)
        assert cl.send_heartbeat(heartbeat_payload(NodeIdentity("n1"), {}, time.time())) is True
    n = app.test_client().get(
        "/api/v1/nodes", headers={"Authorization": f"Bearer {READ}"}
    ).get_json()["nodes"][0]
    assert n["cert"]["expiring"] is True and 11 * DAY < n["cert"]["expires_in_s"] <= 12 * DAY
