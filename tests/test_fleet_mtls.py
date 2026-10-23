"""Per-node mTLS fleet identity: throwaway CAs generated per test, real TLS sockets."""

from __future__ import annotations

import contextlib
import json
import ssl
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from _pki import make_ca, node_cert, server_cert  # noqa: E402

from quietroom.central import (  # noqa: E402
    PEER_CERT_ENVIRON,
    create_central_app,
    make_central_server,
    server_tls_context,
)
from quietroom.config import FleetConfig, NodeConfig, load_config  # noqa: E402
from quietroom.fleet import (  # noqa: E402
    FleetClient,
    NodeIdentity,
    cert_file_identity,
    client_tls_context,
    heartbeat_payload,
    identity_from_peer_cert,
)

U = str(uuid.UUID("6f1c2a52-6a0e-4c35-9a53-0b8a3a6f5e01"))
OTHER = str(uuid.UUID("0d8e8a3b-1c55-4c1f-8f1b-8f0c62b9c9aa"))


class PKI:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.client_ca = make_ca(tmp, "fleet node ca")
        self.server_ca = make_ca(tmp, "fleet central ca")
        self.server_cert, self.server_key = server_cert(self.server_ca, tmp)
        self.node_cert, self.node_key = node_cert(self.client_ca, tmp, U)


@pytest.fixture
def pki(tmp_path):
    return PKI(tmp_path)


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


def _central(pki, **kw):
    return create_central_app(require_client_cert=True, **kw), server_tls_context(
        pki.server_cert, pki.server_key, pki.client_ca.path
    )


def _client(pki, base, node_uuid=U, cert=None, key=None, ca=None, tmp=None):
    ctx = client_tls_context(
        cert or pki.node_cert, key or pki.node_key, ca or pki.server_ca.path
    )
    return FleetClient(base, ssl_context=ctx, node_uuid=node_uuid)


def _hb(label="floor3-east"):
    return heartbeat_payload(NodeIdentity(label, "hq", "3", "lab"), {}, 100.0)


# --- identity extraction -------------------------------------------------------------


def _peer(cn=None, uris=()):
    d: dict = {}
    if cn is not None:
        d["subject"] = ((("commonName", cn),),)
    if uris:
        d["subjectAltName"] = tuple(("URI", u) for u in uris)
    return d


@pytest.mark.parametrize(
    "peer,expected",
    [
        (_peer(uris=(f"urn:uuid:{U}",)), U),
        (_peer(cn=U.upper()), U),  # CN fallback, canonicalised
        (_peer(cn=U, uris=(f"urn:uuid:{U}",)), U),
        (_peer(cn="floor3-east", uris=(f"urn:uuid:{U}",)), U),  # SAN wins over a label CN
        (_peer(cn=OTHER, uris=(f"urn:uuid:{U}",)), None),  # CN and SAN disagree
        (_peer(uris=(f"urn:uuid:{U}", f"urn:uuid:{OTHER}")), None),  # ambiguous
        (_peer(cn="floor3-east"), None),  # no UUID anywhere
        (_peer(uris=("spiffe://x/y",)), None),
        ({}, None),
        (None, None),
    ],
)
def test_identity_from_peer_cert(peer, expected):
    assert identity_from_peer_cert(peer) == expected


def test_cert_file_identity_reads_the_node_cert(pki):
    assert cert_file_identity(pki.node_cert) == U


# --- the wire ------------------------------------------------------------------------


def test_mtls_heartbeat_and_events_are_bound_to_the_cert(pki):
    app, sctx = _central(pki)
    with serving(app, sctx) as base:
        cl = _client(pki, base)
        assert cl.send_heartbeat(_hb()) is True
        assert cl.send_events("floor3-east", [{"segment": "uhf", "findings": []}]) is True
    assert cl.pending() == 0
    node = app.central_nodes[U]
    assert node["auth"] == "mtls" and node["node_uuid"] == U and node["node_id"] == "floor3-east"
    assert app.central_events == [
        {"segment": "uhf", "findings": [], "node_id": "floor3-east", "node_uuid": U, "auth": "mtls"}
    ]


def test_payload_claiming_another_node_uuid_is_rejected(pki):
    app, sctx = _central(pki)
    with serving(app, sctx) as base:
        cl = _client(pki, base, node_uuid=OTHER)  # cert says U, payload says OTHER
        assert cl.send_heartbeat(_hb()) is False
        with pytest.raises(urllib.error.HTTPError) as ei:
            cl._send_direct("/api/v1/heartbeat", json.dumps({**_hb(), "node_uuid": OTHER}))
    assert ei.value.code == 403
    assert app.central_nodes == {} and cl.pending() == 1


def test_event_body_cannot_override_the_bound_identity(pki):
    app, sctx = _central(pki)
    with serving(app, sctx) as base:
        cl = _client(pki, base)
        forged = {"segment": "uhf", "node_uuid": OTHER, "node_id": "ceo-office", "auth": "x"}
        assert cl.send_events("floor3-east", [forged]) is True
    (ev,) = app.central_events
    assert (ev["node_uuid"], ev["node_id"], ev["auth"]) == (U, "floor3-east", "mtls")


def test_cert_from_an_unpinned_ca_fails_the_handshake(pki):
    rogue = make_ca(pki.tmp, "rogue ca")
    rc, rk = node_cert(rogue, pki.tmp, U, stem="rogue-node")
    app, sctx = _central(pki)
    with serving(app, sctx) as base:
        cl = _client(pki, base, cert=rc, key=rk)
        assert cl.send_heartbeat(_hb()) is False
    assert app.central_nodes == {} and cl.pending() == 1


def test_client_without_a_certificate_is_refused(pki):
    app, sctx = _central(pki)
    ctx = ssl.create_default_context(cafile=str(pki.server_ca.path))
    with serving(app, sctx) as base:
        cl = FleetClient(base, ssl_context=ctx, node_uuid=U)
        assert cl.send_heartbeat(_hb()) is False
    assert app.central_nodes == {}


def test_node_pins_the_central_ca(pki):
    rogue = make_ca(pki.tmp, "rogue server ca")
    rs, rk = server_cert(rogue, pki.tmp, stem="rogue-central")
    app = create_central_app(require_client_cert=True)
    with serving(app, server_tls_context(rs, rk, pki.client_ca.path)) as base:
        assert _client(pki, base).send_heartbeat(_hb()) is False
    assert app.central_nodes == {}


def test_client_context_does_not_trust_system_roots(pki):
    ctx = client_tls_context(pki.node_cert, pki.node_key, pki.server_ca.path)
    assert [c["subject"] for c in ctx.get_ca_certs()] == [
        ((("commonName", "fleet central ca"),),)
    ]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_server_context_requires_and_pins_client_certs(pki):
    ctx = server_tls_context(pki.server_cert, pki.server_key, pki.client_ca.path)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert [c["subject"] for c in ctx.get_ca_certs()] == [((("commonName", "fleet node ca"),),)]
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_plain_http_is_refused_for_an_mtls_client(pki):
    ctx = client_tls_context(pki.node_cert, pki.node_key, pki.server_ca.path)
    with pytest.raises(ValueError, match="https"):
        FleetClient("http://central.test", ssl_context=ctx, node_uuid=U)


# --- legacy bearer fallback ----------------------------------------------------------


def test_legacy_bearer_token_is_an_explicit_flagged_fallback(pki):
    tok = "legacy-token-0123456789"
    app = create_central_app(token=tok, require_client_cert=True)
    sctx = server_tls_context(
        pki.server_cert, pki.server_key, pki.client_ca.path, require_client_cert=False
    )
    nocert = ssl.create_default_context(cafile=str(pki.server_ca.path))
    with serving(app, sctx) as base:
        legacy = FleetClient(base, token=tok, ssl_context=nocert)
        assert legacy.send_heartbeat(_hb("old-node")) is True
        wrong = FleetClient(base, token="nope", ssl_context=nocert)
        assert wrong.send_heartbeat(_hb("x")) is False
        # mTLS nodes still work on the same listener during migration
        assert _client(pki, base).send_heartbeat(_hb()) is True
    assert app.central_nodes["old-node"]["auth"] == "legacy-token"
    assert app.central_nodes[U]["auth"] == "mtls"
    c = app.test_client()
    r = c.post(
        "/api/v1/heartbeat", json=_hb("old-node"), headers={"Authorization": f"Bearer {tok}"}
    )
    assert r.headers["X-Quietroom-Auth"] == "legacy-token"


def test_without_a_legacy_token_certless_writes_are_refused():
    app = create_central_app(require_client_cert=True)
    r = app.test_client().post(
        "/api/v1/heartbeat", json=_hb(), headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 403


def test_a_verified_cert_without_a_uuid_is_refused_even_with_a_token():
    app = create_central_app(token="t" * 20, require_client_cert=True)
    r = app.test_client().post(
        "/api/v1/heartbeat",
        json=_hb(),
        headers={"Authorization": "Bearer " + "t" * 20},
        environ_base={PEER_CERT_ENVIRON: _peer(cn="not-a-uuid")},
    )
    assert r.status_code == 403


def test_http_header_cannot_inject_a_peer_cert():
    app = create_central_app(require_client_cert=True)
    r = app.test_client().post(
        "/api/v1/heartbeat",
        json={**_hb(), "node_uuid": U},
        headers={"quietroom.peer_cert": json.dumps(_peer(uris=(f"urn:uuid:{U}",)))},
    )
    assert r.status_code == 403


def test_reads_need_the_operator_token_not_a_node_cert():
    app = create_central_app(require_client_cert=True, read_token="r" * 20, token="t" * 20)
    c = app.test_client()
    as_node = {PEER_CERT_ENVIRON: _peer(uris=(f"urn:uuid:{U}",))}
    assert c.get("/api/v1/nodes", environ_base=as_node).status_code == 403
    legacy = {"Authorization": "Bearer " + "t" * 20}
    assert c.get("/api/v1/nodes", headers=legacy).status_code == 403
    ok = c.get("/api/v1/nodes", headers={"Authorization": "Bearer " + "r" * 20})
    assert ok.status_code == 200
    assert c.get("/healthz").status_code == 200


def test_legacy_only_mode_keeps_old_behaviour():
    app = create_central_app(token="tok1234567890123")
    h = {"Authorization": "Bearer tok1234567890123"}
    c = app.test_client()
    assert c.post("/api/v1/heartbeat", json=_hb("n1"), headers=h).status_code == 200
    assert c.get("/api/v1/nodes", headers=h).get_json()["nodes"][0]["auth"] == "legacy-token"


# --- node config + CLI wiring --------------------------------------------------------

BASE_TOML = """
[node]
id = "floor3-east"
[[segments]]
name = "covert-uhf"
start_hz = 300000000
stop_hz = 320000000
bin_hz = 25000
interval_s = 10
"""


def _cfg(tmp_path, fleet_toml, extra=""):
    p = tmp_path / "node.toml"
    p.write_text(BASE_TOML + extra + "\n[fleet]\n" + fleet_toml)
    return load_config(p)


def _mtls_toml(pki, base="https://central.test:8443", identity=None):
    ident = identity or (pki.tmp / "identity.json")
    return (
        f'base_url = "{base}"\n'
        f'cert_file = "{pki.node_cert}"\nkey_file = "{pki.node_key}"\n'
        f'ca_file = "{pki.server_ca.path}"\nidentity_path = "{ident}"\n'
    )


def test_fleet_config_defaults_to_mtls(pki):
    c = _cfg(pki.tmp, _mtls_toml(pki))
    assert isinstance(c.fleet, FleetConfig)
    assert c.fleet.auth == "mtls" and c.fleet.cert_file == str(pki.node_cert)
    assert c.fleet_base_url == "https://central.test:8443" and c.fleet_heartbeat_s == 60


def test_identity_path_defaults_next_to_the_store(pki):
    t = _mtls_toml(pki).replace(f'identity_path = "{pki.tmp / "identity.json"}"\n', "")
    c = _cfg(pki.tmp, t, extra='[store]\npath = "/var/lib/quietroom/q.db"\n')
    assert c.fleet.identity_path == "/var/lib/quietroom/identity.json"


@pytest.mark.parametrize(
    "fleet_toml,match",
    [
        ('base_url = "https://c.test"\n', "auth = \"legacy-token\""),  # migration hint
        ('base_url = "https://c.test"\nauth = "mtls"\ncert_file = "a"\n', "key_file"),
        (
            'base_url = "http://c.test"\ncert_file="a"\nkey_file="b"\nca_file="c"\n'
            'identity_path="i"\n',
            "https",
        ),
        ('base_url = "https://c.test"\nauth = "kerberos"\n', "auth"),
        (
            'base_url = "https://c.test"\nauth = "legacy-token"\ntoken = "s3cret"\n',
            "QUIETROOM_CENTRAL_TOKEN",
        ),
        ('base_url = "https://c.test"\nauth = "legacy-token"\ncert = "x"\n', "unknown"),
        (
            'base_url = "https://c.test"\ncert_file="a"\nkey_file="b"\nca_file="c"\n',
            "identity_path",
        ),
    ],
)
def test_fleet_config_rejects(tmp_path, fleet_toml, match):
    with pytest.raises(ValueError, match=match):
        _cfg(tmp_path, fleet_toml)


def test_legacy_token_mode_must_be_named(tmp_path):
    c = _cfg(tmp_path, 'base_url = "https://c.test"\nauth = "legacy-token"\n')
    assert c.fleet.auth == "legacy-token"


def test_no_fleet_base_url_means_no_fleet(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text(BASE_TOML)
    assert load_config(p).fleet is None and NodeConfig().fleet is None


def test_build_fleet_mtls_binds_identity_and_never_sends_the_token(pki):
    from quietroom.node.cli import build_fleet

    ident = pki.tmp / "identity.json"
    NodeIdentity.load_or_create(ident, node_id=U)
    cfg = _cfg(pki.tmp, _mtls_toml(pki))
    cl, warnings = build_fleet(cfg, {"QUIETROOM_CENTRAL_TOKEN": "should-not-be-sent"})
    assert cl.node_uuid == U and cl.token is None and cl.ssl_context is not None
    assert warnings == []


def test_build_fleet_refuses_a_cert_for_another_node(pki):
    from quietroom.node.cli import build_fleet

    ident = pki.tmp / "identity.json"
    NodeIdentity.load_or_create(ident, node_id=OTHER)
    with pytest.raises(SystemExit, match=f"{U}.*{OTHER}|{OTHER}.*{U}"):
        build_fleet(_cfg(pki.tmp, _mtls_toml(pki)), {})


def test_build_fleet_warns_on_a_readable_key(pki):
    from quietroom.node.cli import build_fleet

    NodeIdentity.load_or_create(pki.tmp / "identity.json", node_id=U)
    pki.node_key.chmod(0o644)
    _, warnings = build_fleet(_cfg(pki.tmp, _mtls_toml(pki)), {})
    assert any("readable" in w for w in warnings)


def test_build_fleet_legacy_is_flagged_and_needs_the_env_token(tmp_path):
    from quietroom.node.cli import build_fleet

    cfg = _cfg(tmp_path, 'base_url = "https://c.test"\nauth = "legacy-token"\n')
    with pytest.raises(SystemExit, match="QUIETROOM_CENTRAL_TOKEN"):
        build_fleet(cfg, {})
    cl, warnings = build_fleet(cfg, {"QUIETROOM_CENTRAL_TOKEN": "tok"})
    assert cl.token == "tok" and cl.node_uuid is None
    assert any("legacy" in w and "deprecated" in w for w in warnings)


def test_print_identity_creates_and_prints_the_node_uuid(pki, capsys):
    from quietroom.node.cli import main

    p = pki.tmp / "node.toml"
    p.write_text(BASE_TOML + "\n[fleet]\n" + _mtls_toml(pki, identity=pki.tmp / "new-id.json"))
    assert main(["--config", str(p), "--print-identity"]) == 0
    printed = capsys.readouterr().out.strip()
    assert str(uuid.UUID(printed)) == printed
    assert json.loads((pki.tmp / "new-id.json").read_text())["node_id"] == printed
    main(["--config", str(p), "--print-identity"])
    assert capsys.readouterr().out.strip() == printed  # stable


def test_service_flushes_the_fleet_outbox_every_tick():
    from quietroom.config import default_config
    from quietroom.node.service import MonitorService
    from quietroom.radio.recorded import streaming_demo_device

    class Fleet:
        flushed = 0

        def send_events(self, *a):
            return True

        def send_heartbeat(self, *a):
            return True

        def flush(self):
            Fleet.flushed += 1
            return 0

        def pending(self):
            return 3

    svc = MonitorService(default_config(), streaming_demo_device(), fleet=Fleet())
    svc.start()
    svc.tick(now=1e9)
    svc.tick(now=1e9 + 0.5)  # nothing due, still retries the backlog
    assert Fleet.flushed == 2 and svc.health()["fleet_pending"] == 3


def test_node_once_reports_to_central_over_mtls(pki, capsys):
    from quietroom.node.cli import main

    NodeIdentity.load_or_create(pki.tmp / "identity.json", node_id=U)
    app, sctx = _central(pki)
    with serving(app, sctx) as base:
        p = pki.tmp / "node.toml"
        p.write_text(
            BASE_TOML.replace('id = "floor3-east"', 'id = "floor3-east"\nbaseline_cycles = 3')
            + "\n[fleet]\n"
            + _mtls_toml(pki, base=base)
        )
        assert main(["--config", str(p), "--demo", "--once"]) == 0
    health = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["health"]
    assert health["fleet_auth"] == "mtls" and health["fleet_pending"] == 0
    assert app.central_nodes[U]["node_id"] == "floor3-east"
    assert {e["node_uuid"] for e in app.central_events} == {U}


def test_new_payloads_never_overtake_the_backlog(tmp_path):
    # Events, not heartbeats: queued heartbeats coalesce to the newest by design
    # (tests/test_fleet_outbox_bounds.py); events must all arrive, in order.
    seen: list[bytes] = []
    up = {"ok": False}

    def sender(url, body, headers):
        if not up["ok"]:
            raise OSError("down")
        seen.append(json.loads(body)["events"][0]["n"])

    cl = FleetClient("https://c.test", sender=sender, outbox_path=tmp_path / "o.db")
    assert cl.send_events("n1", [{"n": 1}]) is False
    up["ok"] = True
    assert cl.send_events("n1", [{"n": 2}]) is False  # queued behind the backlog, no extra attempt
    assert seen == [] and cl.pending() == 2
    assert cl.flush() == 2 and seen == [1, 2] and cl.pending() == 0
    assert cl.send_events("n1", [{"n": 3}]) is True and seen == [1, 2, 3]


def test_flush_stops_at_the_first_failure_to_keep_order(tmp_path):
    calls = []

    def down(url, body, headers):
        calls.append(body)
        raise OSError("down")

    cl = FleetClient("https://c.test", sender=down, outbox_path=tmp_path / "o.db")
    cl._enqueue("/api/v1/events", b'{"n":1}')
    cl._enqueue("/api/v1/events", b'{"n":2}')
    assert cl.flush() == 0 and len(calls) == 1 and cl.pending() == 2


def test_central_cli_legacy_flag_needs_the_env_token(pki):
    from quietroom.central import main as central_main

    args = ["--cert", str(pki.server_cert), "--key", str(pki.server_key)]
    args += ["--client-ca", str(pki.client_ca.path), "--allow-legacy-token", "--port", "0"]
    with pytest.raises(SystemExit, match="QUIETROOM_CENTRAL_TOKEN"):
        central_main(args, env={})


@pytest.mark.parametrize(
    "legacy,env,verify",
    [
        (False, {}, ssl.CERT_REQUIRED),
        (False, {"QUIETROOM_CENTRAL_READ_TOKEN": "r" * 20}, ssl.CERT_OPTIONAL),
        (True, {"QUIETROOM_CENTRAL_TOKEN": "t" * 20}, ssl.CERT_OPTIONAL),
    ],
)
def test_central_requires_client_certs_at_tls_unless_certless_access_is_configured(
    pki, legacy, env, verify
):
    from quietroom.central import build_central

    app, ctx, mode = build_central(
        str(pki.server_cert), str(pki.server_key), str(pki.client_ca.path),
        allow_legacy_token=legacy, env=env,
    )
    assert ctx.verify_mode == verify
    assert ("DEPRECATED" in mode) is legacy
    # whatever the TLS mode, a certificate-less write without the legacy token is refused
    r = app.test_client().post("/api/v1/heartbeat", json=_hb())
    assert r.status_code == 403


def test_certless_operator_read_works_over_tls_when_read_token_set(pki):
    from quietroom.central import build_central

    rt = "r" * 20
    app, ctx, _ = build_central(
        str(pki.server_cert), str(pki.server_key), str(pki.client_ca.path),
        allow_legacy_token=False, env={"QUIETROOM_CENTRAL_READ_TOKEN": rt},
    )
    with serving(app, ctx) as base:
        assert _client(pki, base).send_heartbeat(_hb()) is True
        op = ssl.create_default_context(cafile=str(pki.server_ca.path))
        req = urllib.request.Request(
            f"{base}/api/v1/nodes", headers={"Authorization": f"Bearer {rt}"}
        )
        with urllib.request.urlopen(req, context=op, timeout=5) as r:
            nodes = json.load(r)["nodes"]
        nocert_writer = FleetClient(base, ssl_context=op, node_uuid=U)
        assert nocert_writer.send_heartbeat(_hb("impostor")) is False
    assert [n["node_uuid"] for n in nodes] == [U]
    assert app.central_nodes[U]["node_id"] == "floor3-east"
