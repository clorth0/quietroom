import pytest

from quietroom.web import app as webapp
from quietroom.web.app import create_app

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


def test_main_refuses_non_loopback_host_without_opt_in(monkeypatch):
    monkeypatch.setattr(webapp, "_run", lambda *a, **k: pytest.fail("should not run"))
    with pytest.raises(SystemExit):
        webapp.main(["--demo", "--host", "0.0.0.0"])


def test_main_remote_requires_token(monkeypatch):
    monkeypatch.delenv("QUIETROOM_TOKEN", raising=False)
    monkeypatch.setattr(webapp, "_run", lambda *a, **k: pytest.fail("should not run"))
    with pytest.raises(SystemExit):
        webapp.main(["--demo", "--host", "0.0.0.0", "--allow-remote"])


def test_main_remote_with_token_runs(monkeypatch):
    ran = {}
    monkeypatch.setenv("QUIETROOM_TOKEN", "s3cret-token-value")
    monkeypatch.setattr(webapp, "_run", lambda socketio, app, host, port: ran.update(host=host))
    assert webapp.main(["--demo", "--host", "100.64.0.5", "--allow-remote"]) == 0
    assert ran["host"] == "100.64.0.5"


def test_token_required_when_configured():
    app, _ = create_app(demo=True, token="s3cret-token-value")
    c = app.test_client()
    assert c.get("/").status_code == 403
    assert c.get("/?token=wrong").status_code == 403
    ok = c.get("/?token=s3cret-token-value")
    assert ok.status_code == 200
    assert c.get("/").status_code == 200           # cookie set on success


def test_socket_connect_rejected_without_token():
    app, socketio = create_app(demo=True, token="s3cret-token-value")
    client = socketio.test_client(app)
    assert not client.is_connected()
    client = socketio.test_client(app, query_string="token=s3cret-token-value")
    assert client.is_connected()
