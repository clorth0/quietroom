"""--check-config: every env secret the config needs is present; values never printed."""

import json
import os
import uuid

import pytest

pytest.importorskip("cryptography")

from quietroom import central  # noqa: E402
from quietroom.node.cli import main  # noqa: E402

from _pki import make_ca, node_cert, server_cert  # noqa: E402

SECRET = "s3cr3t-VALUE-never-print-me"
SEG = '[[segments]]\nname = "s"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n'
EMAIL_AUTH = (
    '[alerts.email]\nhost = "smtp.test"\nfrom = "a@x.test"\nto = "b@x.test"\n'
    'username = "qr"\n'
)


def write(tmp_path, body):
    p = tmp_path / "node.toml"
    p.write_text(body + SEG)
    return p


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("QUIETROOM_"):
            monkeypatch.delenv(k)
    return monkeypatch


def check(conf, capsys):
    rc = main(["--config", str(conf), "--check-config"])
    out, err = capsys.readouterr()
    assert SECRET not in out and SECRET not in err
    return rc, json.loads(out), err


def secret(report, env):
    [s] = [s for s in report["secrets"] if s["env"] == env]
    return s


def test_nothing_needed_is_ok(tmp_path, capsys, clean_env):
    rc, r, _ = check(write(tmp_path, '[node]\nid = "n1"\n'), capsys)
    assert rc == 0 and r["ok"] is True and r["secrets"] == [] and r["errors"] == []
    assert r["config"]["node_id"] == "n1"


def test_smtp_password_required_when_a_username_is_set(tmp_path, capsys, clean_env):
    conf = write(tmp_path, "[alerts]\n" + EMAIL_AUTH)
    rc, r, err = check(conf, capsys)
    assert rc == 1 and r["ok"] is False
    assert secret(r, "QUIETROOM_SMTP_PASSWORD") == {
        "env": "QUIETROOM_SMTP_PASSWORD", "for": "[alerts.email] SMTP login 'qr'",
        "required": True, "present": False,
    }
    assert "QUIETROOM_SMTP_PASSWORD" in err and "ERROR" in err
    clean_env.setenv("QUIETROOM_SMTP_PASSWORD", SECRET)
    rc, r, _ = check(conf, capsys)
    assert rc == 0 and secret(r, "QUIETROOM_SMTP_PASSWORD")["present"] is True


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_secrets_count_as_missing(tmp_path, capsys, clean_env, blank):
    clean_env.setenv("QUIETROOM_SMTP_PASSWORD", blank)
    rc, r, _ = check(write(tmp_path, "[alerts]\n" + EMAIL_AUTH), capsys)
    assert rc == 1 and secret(r, "QUIETROOM_SMTP_PASSWORD")["present"] is False


def test_unauthenticated_relay_needs_no_password(tmp_path, capsys, clean_env):
    body = '[alerts.email]\nhost = "127.0.0.1"\nfrom = "a@x.test"\nto = "b@x.test"\n'
    rc, r, _ = check(write(tmp_path, body), capsys)
    assert rc == 0 and r["secrets"] == []


def test_webhook_secret_is_recommended_not_required(tmp_path, capsys, clean_env):
    conf = write(tmp_path, '[alerts]\nwebhook_url = "https://hook.test/x"\n')
    rc, r, err = check(conf, capsys)
    s = secret(r, "QUIETROOM_WEBHOOK_SECRET")
    assert rc == 0 and s["required"] is False and s["present"] is False
    assert any("unsigned" in w for w in r["warnings"]) and "WARNING" in err
    clean_env.setenv("QUIETROOM_WEBHOOK_SECRET", SECRET)
    rc, r, _ = check(conf, capsys)
    assert rc == 0 and secret(r, "QUIETROOM_WEBHOOK_SECRET")["present"] is True
    assert r["warnings"] == []


def test_legacy_token_required_for_legacy_fleet_auth(tmp_path, capsys, clean_env):
    conf = write(
        tmp_path,
        f'[store]\npath = "{tmp_path / "q.db"}"\n'
        '[fleet]\nbase_url = "https://central.test"\nauth = "legacy-token"\n',
    )
    rc, r, _ = check(conf, capsys)
    assert rc == 1 and secret(r, "QUIETROOM_CENTRAL_TOKEN")["required"] is True
    clean_env.setenv("QUIETROOM_CENTRAL_TOKEN", SECRET)
    rc, r, _ = check(conf, capsys)
    assert rc == 0 and secret(r, "QUIETROOM_CENTRAL_TOKEN")["present"] is True
    assert any("deprecated" in w for w in r["warnings"])


@pytest.fixture
def mtls(tmp_path):
    ca = make_ca(tmp_path, "ca")
    u = str(uuid.uuid4())
    cert, key = node_cert(ca, tmp_path, u)
    ident = tmp_path / "identity.json"
    ident.write_text(json.dumps({"node_id": u}))

    def conf(**over):
        f = {"cert_file": cert, "key_file": key, "ca_file": ca.path, "identity_path": ident}
        f.update(over)
        keys = "".join(f'{k} = "{v}"\n' for k, v in f.items())
        return write(
            tmp_path,
            f'[fleet]\nbase_url = "https://central.test"\nauth = "mtls"\n{keys}',
        )

    return conf, key, ident, tmp_path


def test_mtls_material_ok(mtls, capsys, clean_env):
    conf, *_ = mtls
    rc, r, _ = check(conf(), capsys)
    assert rc == 0, r
    assert {c["check"] for c in r["checks"] if c["ok"]} >= {
        "fleet mTLS material loads", "client certificate UUID matches identity"
    }


def test_mtls_flags_a_stray_legacy_token_and_loose_key(mtls, capsys, clean_env):
    conf, key, *_ = mtls
    os.chmod(key, 0o644)
    clean_env.setenv("QUIETROOM_CENTRAL_TOKEN", SECRET)
    rc, r, _ = check(conf(), capsys)
    assert rc == 0
    assert any("QUIETROOM_CENTRAL_TOKEN" in w and "ignored" in w for w in r["warnings"])
    assert any("0600" in w for w in r["warnings"])


def test_mtls_missing_files_and_mismatched_uuid_are_errors(mtls, capsys, clean_env):
    conf, _, ident, tmp = mtls
    rc, r, _ = check(conf(cert_file=tmp / "nope.crt"), capsys)
    assert rc == 1 and any("nope.crt" in e for e in r["errors"])
    ident.write_text(json.dumps({"node_id": str(uuid.uuid4())}))
    rc, r, _ = check(conf(), capsys)
    assert rc == 1 and any("is for node" in e for e in r["errors"])


def test_check_config_never_creates_the_identity_file(mtls, capsys, clean_env):
    conf, _, ident, _ = mtls
    ident.unlink()
    rc, r, _ = check(conf(), capsys)
    assert not ident.exists()
    assert rc == 1 and any("--print-identity" in e for e in r["errors"])


# --- quietroom-central --check-config --------------------------------------


@pytest.fixture
def central_pki(tmp_path):
    ca = make_ca(tmp_path, "ca")
    cert, key = server_cert(ca, tmp_path)
    return ["--cert", str(cert), "--key", str(key), "--client-ca", str(ca.path)]


def ccheck(argv, env, capsys):
    rc = central.main([*argv, "--check-config"], env=env)
    out, err = capsys.readouterr()
    assert SECRET not in out and SECRET not in err
    return rc, json.loads(out)


def test_central_check_config_ok_and_read_token_recommended(central_pki, capsys):
    rc, r = ccheck(central_pki, {}, capsys)
    assert rc == 0 and r["ok"] is True
    rt = [s for s in r["secrets"] if s["env"] == "QUIETROOM_CENTRAL_READ_TOKEN"][0]
    assert rt["required"] is False and rt["present"] is False
    assert any("operator reads" in w for w in r["warnings"])


def test_central_check_config_legacy_token(central_pki, capsys):
    rc, r = ccheck([*central_pki, "--allow-legacy-token"], {}, capsys)
    assert rc == 1 and any("QUIETROOM_CENTRAL_TOKEN" in e for e in r["errors"])
    env = {"QUIETROOM_CENTRAL_TOKEN": SECRET, "QUIETROOM_CENTRAL_READ_TOKEN": SECRET + "r"}
    rc, r = ccheck([*central_pki, "--allow-legacy-token"], env, capsys)
    assert rc == 0 and all(s["present"] for s in r["secrets"])
    rc, r = ccheck(central_pki, {"QUIETROOM_CENTRAL_TOKEN": SECRET}, capsys)
    assert any("ignored" in w for w in r["warnings"])  # set but legacy not enabled


def test_central_check_config_bad_tls_material(central_pki, tmp_path, capsys):
    argv = list(central_pki)
    argv[argv.index("--key") + 1] = str(tmp_path / "missing.key")
    rc, r = ccheck(argv, {}, capsys)
    assert rc == 1 and any("missing.key" in e for e in r["errors"])
