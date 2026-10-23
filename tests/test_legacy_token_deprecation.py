"""The legacy shared token stays (deprecated) with a stated removal plan, loudly."""

from __future__ import annotations

import email.utils

import pytest

from quietroom.central import create_central_app
from quietroom.fleet import (
    LEGACY_TOKEN_DEPRECATED_AT,
    LEGACY_TOKEN_REMOVAL,
    LEGACY_TOKEN_SUNSET,
    NodeIdentity,
    heartbeat_payload,
)

TOK = "legacy-token-0123456789"
SEG = (
    '[[segments]]\nname = "uhf"\nstart_hz = 300000000\nstop_hz = 320000000\nbin_hz = 25000\n'
)


def test_the_removal_plan_names_a_version_and_a_date():
    assert "0.5.0" in LEGACY_TOKEN_REMOVAL and "2027-04-01" in LEGACY_TOKEN_REMOVAL


def test_legacy_writes_carry_deprecation_and_sunset_headers_and_are_counted():
    app = create_central_app(TOK, require_client_cert=True)
    c = app.test_client()
    r = c.post(
        "/api/v1/heartbeat",
        json=heartbeat_payload(NodeIdentity("n1"), {}, 1.0),
        headers={"Authorization": f"Bearer {TOK}"},
    )
    assert r.status_code == 200 and r.headers["X-Quietroom-Auth"] == "legacy-token"
    assert r.headers["Deprecation"] == f"@{int(LEGACY_TOKEN_DEPRECATED_AT)}"  # RFC 9745
    sunset = email.utils.parsedate_to_datetime(r.headers["Sunset"])  # RFC 8594 HTTP-date
    assert sunset.timestamp() == LEGACY_TOKEN_SUNSET
    assert 'rel="deprecation"' in r.headers["Link"]
    assert c.get("/healthz").get_json()["legacy_token_writes"] == 1


def test_mtls_responses_carry_no_deprecation_headers():
    from quietroom.central import PEER_CERT_ENVIRON

    u = "6f1c2a52-6a0e-4c35-9a53-0b8a3a6f5e01"
    app = create_central_app(TOK, require_client_cert=True)
    r = app.test_client().post(
        "/api/v1/heartbeat",
        json={**heartbeat_payload(NodeIdentity("n1"), {}, 1.0), "node_uuid": u},
        environ_base={PEER_CERT_ENVIRON: {"subjectAltName": (("URI", f"urn:uuid:{u}"),)}},
    )
    assert r.status_code == 200 and "Deprecation" not in r.headers and "Sunset" not in r.headers
    assert app.test_client().get("/healthz").get_json()["legacy_token_writes"] == 0


def test_central_warns_on_stderr_at_startup_with_the_removal_plan(tmp_path, capsys):
    pytest.importorskip("cryptography")
    from _pki import make_ca, server_cert

    from quietroom.central import build_central

    ca = make_ca(tmp_path, "node ca")
    sc, sk = server_cert(make_ca(tmp_path, "srv ca"), tmp_path)
    build_central(
        str(sc), str(sk), str(ca.path), allow_legacy_token=True,
        env={"QUIETROOM_CENTRAL_TOKEN": TOK},
    )
    err = capsys.readouterr().err
    assert "DEPRECATED" in err and LEGACY_TOKEN_REMOVAL in err and TOK not in err


def test_node_legacy_mode_warning_states_the_removal_plan(tmp_path):
    from quietroom.config import load_config
    from quietroom.node.cli import build_fleet

    p = tmp_path / "n.toml"
    p.write_text(SEG + '[fleet]\nbase_url = "https://c.test"\nauth = "legacy-token"\n')
    _, warnings = build_fleet(load_config(p), {"QUIETROOM_CENTRAL_TOKEN": TOK})
    (w,) = [w for w in warnings if "legacy" in w]
    assert LEGACY_TOKEN_REMOVAL in w and TOK not in w


def test_node_prints_fleet_warnings_to_stderr_at_startup(tmp_path, capsys, monkeypatch):
    from quietroom.node import cli

    p = tmp_path / "n.toml"
    # port 9 on loopback: refused immediately, so --once doesn't wait on a network timeout
    p.write_text(SEG + '[fleet]\nbase_url = "https://127.0.0.1:9"\nauth = "legacy-token"\n')
    monkeypatch.setenv("QUIETROOM_CENTRAL_TOKEN", TOK)
    assert cli.main(["--config", str(p), "--demo", "--once"]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and LEGACY_TOKEN_REMOVAL in err and TOK not in err


def test_the_docs_state_the_same_plan_and_header_values():
    from pathlib import Path

    doc = (Path(__file__).parents[1] / "packaging" / "fleet-mtls.md").read_text()
    assert "quietroom 0.5.0,\nnot before 2027-04-01" in doc  # the plan, as stated in the docs
    assert f"Deprecation: @{int(LEGACY_TOKEN_DEPRECATED_AT)}" in doc
    assert email.utils.formatdate(LEGACY_TOKEN_SUNSET, usegmt=True) in doc
