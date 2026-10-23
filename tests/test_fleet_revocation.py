"""UUID deny-list at central: checked per request, reloaded on change, fails closed."""

from __future__ import annotations

import os
import uuid

import pytest

from quietroom.central import (
    PEER_CERT_ENVIRON,
    DenyList,
    build_central,
    create_central_app,
    parse_deny_list,
)
from quietroom.fleet import NodeIdentity, heartbeat_payload

U = "6f1c2a52-6a0e-4c35-9a53-0b8a3a6f5e01"
OTHER = "0d8e8a3b-1c55-4c1f-8f1b-8f0c62b9c9aa"
READ = "read-token-0123456789"


def _peer(u):
    return {
        "subject": ((("commonName", u),),),
        "subjectAltName": (("URI", f"urn:uuid:{u}"),),
        "notAfter": "Jan  1 00:00:00 2099 GMT",
    }


def _hb(u, label="n"):
    return {**heartbeat_payload(NodeIdentity(label), {}, 100.0), "node_uuid": u}


def _write(path, text):
    """Replace atomically, the way the docs tell operators to edit the list."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _app(tmp_path, text="", **kw):
    p = tmp_path / "deny.txt"
    p.write_text(text)
    app = create_central_app(
        require_client_cert=True, read_token=READ, deny_list=DenyList(p), **kw
    )
    return app, app.test_client(), p


def _post(c, u, path="/api/v1/heartbeat", body=None):
    return c.post(path, json=body or _hb(u), environ_base={PEER_CERT_ENVIRON: _peer(u)})


def test_parse_deny_list_canonicalises_and_ignores_comments():
    text = f"# revoked 2026-10-22: laptop stolen\n{U.upper()}  # floor 3\n\n  urn:uuid:{OTHER}\n"
    assert parse_deny_list(text) == frozenset({U, OTHER})


@pytest.mark.parametrize("bad", ["not-a-uuid", f"{U} extra", "123"])
def test_parse_deny_list_rejects_malformed_lines_with_line_numbers(bad):
    with pytest.raises(ValueError, match="line 2"):
        parse_deny_list(f"# ok\n{bad}\n")


def test_a_denied_uuid_is_refused_on_every_write_others_are_not(tmp_path):
    app, c, _ = _app(tmp_path, f"{U}\n")
    r = _post(c, U)
    assert r.status_code == 403 and b"revoked" in r.data
    assert _post(c, U, "/api/v1/events", {"node_uuid": U, "events": [{"x": 1}]}).status_code == 403
    assert _post(c, OTHER).status_code == 200
    assert set(app.central_nodes) == {OTHER} and app.central_events == []


def test_editing_the_list_takes_effect_on_the_next_request_without_restart(tmp_path):
    app, c, p = _app(tmp_path, "")
    assert _post(c, U).status_code == 200
    _write(p, f"# revoked\n{U}\n")
    assert _post(c, U).status_code == 403
    _write(p, "")  # un-revoke
    assert _post(c, U).status_code == 200


def test_in_place_edit_of_the_same_size_is_still_noticed(tmp_path):
    app, c, p = _app(tmp_path, f"{OTHER}\n")
    assert _post(c, U).status_code == 200
    st = p.stat()
    p.write_text(f"{U}\n")  # same length, in place
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert _post(c, U).status_code == 403


def test_the_list_is_not_reread_when_unchanged(tmp_path, monkeypatch):
    app, c, p = _app(tmp_path, f"{OTHER}\n")
    reads = []
    real = DenyList._read
    monkeypatch.setattr(DenyList, "_read", lambda self: reads.append(1) or real(self))
    for _ in range(5):
        _post(c, U)
    assert reads == []


@pytest.mark.parametrize("breakage", ["malformed", "deleted"])
def test_a_broken_list_fails_closed_for_writes_and_recovers(tmp_path, breakage):
    app, c, p = _app(tmp_path, "")
    if breakage == "malformed":
        _write(p, "oops\n")
    else:
        p.unlink()
    r = _post(c, OTHER)
    assert r.status_code == 503 and b"deny list" in r.data
    h = c.get("/healthz").get_json()
    assert h["ok"] is False and h["deny_list"]["error"]
    # operator reads keep working while writes are paused
    assert c.get("/api/v1/nodes", headers={"Authorization": f"Bearer {READ}"}).status_code == 200
    _write(p, f"{U}\n")
    assert _post(c, OTHER).status_code == 200 and _post(c, U).status_code == 403
    assert c.get("/healthz").get_json()["deny_list"] == {
        "path": str(p), "entries": 1, "error": None
    }


def test_a_broken_list_also_pauses_legacy_token_writes(tmp_path):
    p = tmp_path / "deny.txt"
    p.write_text("")
    app = create_central_app(
        "legacy-token-0123456789", require_client_cert=True, deny_list=DenyList(p)
    )
    c = app.test_client()
    auth = {"Authorization": "Bearer legacy-token-0123456789"}
    assert c.post("/api/v1/heartbeat", json=_hb(U), headers=auth).status_code == 200
    _write(p, "garbage\n")
    assert c.post("/api/v1/heartbeat", json=_hb(U), headers=auth).status_code == 503


def test_nodes_listing_marks_revoked_nodes(tmp_path):
    app, c, p = _app(tmp_path, "")
    assert _post(c, U).status_code == 200 and _post(c, OTHER).status_code == 200
    _write(p, f"{U}\n")
    nodes = c.get("/api/v1/nodes", headers={"Authorization": f"Bearer {READ}"}).get_json()
    assert {n["node_uuid"]: n["revoked"] for n in nodes["nodes"]} == {U: True, OTHER: False}


def test_a_missing_or_malformed_list_at_startup_is_fatal(tmp_path):
    with pytest.raises(ValueError, match="deny list"):
        DenyList(tmp_path / "nope.txt")
    bad = tmp_path / "bad.txt"
    bad.write_text("nope\n")
    with pytest.raises(ValueError, match="line 1"):
        DenyList(bad)


def test_deny_list_needs_client_certificates(tmp_path):
    p = tmp_path / "deny.txt"
    p.write_text("")
    with pytest.raises(ValueError, match="client cert"):
        create_central_app("t" * 20, deny_list=DenyList(p))


def test_build_central_wires_the_deny_list_and_refuses_a_missing_file(tmp_path):
    pytest.importorskip("cryptography")
    from _pki import make_ca, server_cert

    ca = make_ca(tmp_path, "node ca")
    sc, sk = server_cert(make_ca(tmp_path, "srv ca"), tmp_path)
    with pytest.raises(SystemExit, match="deny"):
        build_central(
            str(sc), str(sk), str(ca.path), allow_legacy_token=False, env={},
            deny_list=str(tmp_path / "missing.txt"),
        )
    p = tmp_path / "deny.txt"
    p.write_text(f"{uuid.UUID(U)}\n")
    app, _, mode = build_central(
        str(sc), str(sk), str(ca.path), allow_legacy_token=False, env={}, deny_list=str(p)
    )
    assert "deny list: 1" in mode
    assert _post(app.test_client(), U).status_code == 403
