"""quietroom-central --check-config validates --deny-list and --cert-warn-days."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")

from _pki import make_ca, server_cert  # noqa: E402

from quietroom.central import (  # noqa: E402
    CERT_WARN_DAYS_MAX,
    check_central_config,
    main,
    parse_cert_warn_days,
)

U1 = "6f1c2a52-6a0e-4c35-9a53-0b8a3a6f5e01"
U2 = "0d8e8a3b-1c55-4c1f-8f1b-8f0c62b9c9aa"


@pytest.fixture
def tls(tmp_path):
    client_ca = make_ca(tmp_path, "fleet node ca")
    srv_ca = make_ca(tmp_path, "fleet central ca")
    cert, key = server_cert(srv_ca, tmp_path)
    return str(cert), str(key), str(client_ca.path)


def _check(tls, **kw):
    cert, key, ca = tls
    return check_central_config(cert, key, ca, allow_legacy_token=False, env={}, **kw)


def test_without_the_options_nothing_changes(tls):
    r = _check(tls)
    assert r["ok"], r["errors"]


def test_a_valid_deny_list_passes_and_is_counted(tls, tmp_path):
    p = tmp_path / "deny.txt"
    p.write_text(f"# revoked\n{U1}\n{U2.upper()}  # lost laptop\n\n")
    r = _check(tls, deny_list=str(p))
    assert r["ok"], r["errors"]
    assert any("deny list" in c["check"] and c["ok"] and "2 revoked" in c["detail"]
               for c in r["checks"]), r["checks"]


def test_an_empty_deny_list_is_valid(tls, tmp_path):
    p = tmp_path / "deny.txt"
    p.write_text("# nothing revoked yet\n")
    assert _check(tls, deny_list=str(p))["ok"]


def test_a_missing_deny_list_is_an_error(tls, tmp_path):
    r = _check(tls, deny_list=str(tmp_path / "nope.txt"))
    assert not r["ok"]
    assert any("--deny-list" in e and "nope.txt" in e for e in r["errors"]), r["errors"]


def test_a_deny_list_that_is_a_directory_is_an_error(tls, tmp_path):
    r = _check(tls, deny_list=str(tmp_path))
    assert not r["ok"]
    assert any("--deny-list" in e for e in r["errors"]), r["errors"]


def test_a_deny_list_with_a_bad_line_names_the_line(tls, tmp_path):
    p = tmp_path / "deny.txt"
    p.write_text(f"{U1}\nnot-a-uuid\n")
    r = _check(tls, deny_list=str(p))
    assert not r["ok"]
    [e] = [e for e in r["errors"] if "--deny-list" in e]
    assert "line 2" in e and "not-a-uuid" in e


def test_a_deny_list_that_is_not_text_is_an_error(tls, tmp_path):
    p = tmp_path / "deny.txt"
    p.write_bytes(b"\xff\xfe\x00garbage")
    r = _check(tls, deny_list=str(p))
    assert not r["ok"]
    assert any("--deny-list" in e for e in r["errors"]), r["errors"]


@pytest.mark.parametrize("value", ["1", "30", "365", 30])
def test_cert_warn_days_in_range_passes(tls, value):
    assert _check(tls, cert_warn_days=value)["ok"]
    assert parse_cert_warn_days(value) == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "366", "1.5", "abc", "", "1e3", True, 0, 400])
def test_cert_warn_days_out_of_range_or_not_an_int_is_an_error(tls, value):
    r = _check(tls, cert_warn_days=value)
    assert not r["ok"]
    [e] = [e for e in r["errors"] if "--cert-warn-days" in e]
    assert f"1 to {CERT_WARN_DAYS_MAX}" in e and "integer" in e
    with pytest.raises(ValueError):
        parse_cert_warn_days(value)


def test_check_config_cli_reports_both_and_exits_1(tls, tmp_path, capsys):
    cert, key, ca = tls
    p = tmp_path / "deny.txt"
    p.write_text("bogus\n")
    rc = main(["--cert", cert, "--key", key, "--client-ca", ca, "--check-config",
               "--deny-list", str(p), "--cert-warn-days", "0"], env={})
    out, err = capsys.readouterr()
    assert rc == 1
    report = json.loads(out)
    assert not report["ok"]
    assert sum("--deny-list" in e for e in report["errors"]) == 1
    assert sum("--cert-warn-days" in e for e in report["errors"]) == 1
    assert "Traceback" not in err and "ERROR: --cert-warn-days" in err


def test_check_config_cli_passes_with_valid_options(tls, tmp_path, capsys):
    cert, key, ca = tls
    p = tmp_path / "deny.txt"
    p.write_text(f"{U1}\n")
    rc = main(["--cert", cert, "--key", key, "--client-ca", ca, "--check-config",
               "--deny-list", str(p), "--cert-warn-days", "14"], env={})
    assert rc == 0, capsys.readouterr()


@pytest.mark.parametrize("value", ["0", "1000", "x"])
def test_serving_refuses_a_bad_cert_warn_days(tls, value, capsys):
    cert, key, ca = tls
    with pytest.raises(SystemExit) as exc:
        main(["--cert", cert, "--key", key, "--client-ca", ca, "--cert-warn-days", value],
             env={})
    assert exc.value.code == 2
    assert "--cert-warn-days" in capsys.readouterr().err
