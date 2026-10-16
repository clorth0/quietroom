import ast
from pathlib import Path

import pytest

from quietroom.web import app as webapp
from quietroom.web.app import create_app
from quietroom.web.bind import LocalBind, RemoteBind

POLL = "/socket.io/?EIO=4&transport=polling"


def test_cross_site_origin_cannot_open_socket():
    # A page on another site must not be able to drive the sweep/speaker.
    app, _ = create_app(demo=True)
    r = app.test_client().get(POLL, headers={"Origin": "https://evil.example"})
    assert r.status_code == 400


def test_same_origin_socket_is_allowed():
    app, _ = create_app(demo=True)
    r = app.test_client().get(POLL, headers={"Origin": "http://127.0.0.1:8770"},
                              base_url="http://127.0.0.1:8770")
    assert r.status_code == 200


def test_dns_rebinding_host_is_rejected():
    app, _ = create_app(demo=True)
    r = app.test_client().get("/", base_url="http://evil.example:8770")
    assert r.status_code == 403


def test_main_refuses_non_loopback_host_without_opt_in(monkeypatch, capsys):
    monkeypatch.setattr(webapp, "_run", lambda *a, **k: pytest.fail("should not run"))
    with pytest.raises(SystemExit):
        webapp.main(["--demo", "--host", "0.0.0.0"])
    err = capsys.readouterr().err
    assert "refusing to bind 0.0.0.0: pass --allow-remote" in err
    assert "prefer an SSH tunnel or Tailscale to 127.0.0.1" in err


def test_main_remote_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("QUIETROOM_TOKEN", raising=False)
    monkeypatch.setattr(webapp, "_run", lambda *a, **k: pytest.fail("should not run"))
    with pytest.raises(SystemExit):
        webapp.main(["--demo", "--host", "0.0.0.0", "--allow-remote"])
    assert "--allow-remote needs QUIETROOM_TOKEN set (16+ chars)" in capsys.readouterr().err


def test_main_remote_with_token_runs(monkeypatch):
    ran = {}
    monkeypatch.setenv("QUIETROOM_TOKEN", "s3cret-token-value")
    monkeypatch.setattr(webapp, "_run", lambda socketio, app, host, port: ran.update(host=host))
    assert webapp.main(["--demo", "--host", "100.64.0.5", "--allow-remote"]) == 0
    assert ran["host"] == "100.64.0.5"


def test_remote_token_must_be_16_characters(monkeypatch):
    monkeypatch.setattr(webapp, "_run", lambda *a, **k: None)
    monkeypatch.setenv("QUIETROOM_TOKEN", "a" * 15)
    with pytest.raises(SystemExit):
        webapp.main(["--demo", "--host", "0.0.0.0", "--allow-remote"])
    monkeypatch.setenv("QUIETROOM_TOKEN", "b" * 16)
    assert webapp.main(["--demo", "--host", "0.0.0.0", "--allow-remote"]) == 0
    with pytest.raises(ValueError, match="16"):
        RemoteBind("0.0.0.0", "c" * 15)


def test_token_required_when_configured():
    app, _ = create_app(demo=True, bind=LocalBind(token="s3cret-token-value"))
    c = app.test_client()
    assert c.get("/").status_code == 403
    assert c.get("/?token=wrong").status_code == 403
    ok = c.get("/?token=s3cret-token-value")
    assert ok.status_code == 200
    assert c.get("/").status_code == 200           # cookie set on success


def _cors_values(source: str) -> list[ast.AST]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "cors_allowed_origins":
                found.append(kw.value)
    return found


def assert_cors_is_bind_callable(source: str) -> None:
    values = _cors_values(source)
    assert values, "expected a cors_allowed_origins argument"
    for value in values:
        if isinstance(value, ast.List) or (
            isinstance(value, ast.Constant) and isinstance(value.value, str)
        ):
            raise AssertionError(
                "cors_allowed_origins must not be a list or a string; "
                "pass the callable from quietroom.web.bind"
            )


def test_socketio_cors_is_the_bind_callable():
    source = Path("quietroom/web/app.py").read_text()
    values = _cors_values(source)
    assert len(values) == 1
    assert isinstance(values[0], ast.Name)
    assert_cors_is_bind_callable(source)


def test_cors_list_and_string_fail_the_ast_rule():
    for bad in ("[]", '"*"', "['http://evil.example']"):
        with pytest.raises(AssertionError, match="quietroom.web.bind"):
            assert_cors_is_bind_callable(f"SocketIO(app, cors_allowed_origins={bad})")


def test_socket_connect_rejected_without_token():
    app, socketio = create_app(demo=True, bind=LocalBind(token="s3cret-token-value"))
    client = socketio.test_client(app)
    assert not client.is_connected()
    client = socketio.test_client(app, query_string="token=s3cret-token-value")
    assert client.is_connected()
