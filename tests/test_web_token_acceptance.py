"""Characterization: both token checks' current accept/reject matrix.

Pins HTTP (app._guard) and Socket.IO connect (commands dispatch) behaviour
before the two checks are unified. A wrong query token shadows a good
cookie on both surfaces; a wrong socket ``auth`` token does not shadow
the query/cookie fallback. X-Forwarded-For is not consulted anywhere.
"""
from quietroom.web.app import create_app
from quietroom.web.bind import LocalBind

TOKEN = "s3cret-token-value"


def _http():
    app, _ = create_app(demo=True, bind=LocalBind(token=TOKEN))
    return app.test_client()


def _socket(**kwargs):
    app, socketio = create_app(demo=True, bind=LocalBind(token=TOKEN))
    return socketio.test_client(app, **kwargs)


# --- HTTP surface -----------------------------------------------------

def test_http_missing_or_wrong_token_is_rejected():
    assert _http().get("/").status_code == 403
    assert _http().get("/?token=wrong").status_code == 403
    assert _http().get("/?token=").status_code == 403


def test_http_query_token_accepted_and_sets_cookie():
    c = _http()
    assert c.get(f"/?token={TOKEN}").status_code == 200
    assert c.get("/").status_code == 200  # cookie set by the query success


def test_http_cookie_alone_accepted_wrong_cookie_rejected():
    c = _http()
    c.set_cookie("quietroom_token", TOKEN)
    assert c.get("/").status_code == 200
    c.set_cookie("quietroom_token", "wrong")
    assert c.get("/").status_code == 403


def test_http_wrong_query_shadows_good_cookie():
    # request.args wins over the cookie: a wrong query is a rejection,
    # not a chance to fall through to the cookie.
    c = _http()
    c.set_cookie("quietroom_token", TOKEN)
    assert c.get("/?token=wrong").status_code == 403


def test_http_forwarded_for_header_changes_nothing():
    c = _http()
    assert c.get("/", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert c.get(
        f"/?token={TOKEN}", headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
    ).status_code == 200


# --- Socket.IO connect surface ---------------------------------------

def test_socket_missing_or_wrong_auth_is_rejected():
    assert not _socket().is_connected()
    assert not _socket(auth={"token": "wrong"}).is_connected()
    assert not _socket(auth={}).is_connected()
    assert not _socket(auth="not-a-dict").is_connected()


def test_socket_auth_dict_token_accepted():
    client = _socket(auth={"token": TOKEN})
    assert client.is_connected()
    client.disconnect()


def test_socket_query_and_cookie_fallbacks_accepted():
    client = _socket(query_string=f"token={TOKEN}")
    assert client.is_connected()
    client.disconnect()
    client = _socket(headers={"Cookie": f"quietroom_token={TOKEN}"})
    assert client.is_connected()
    client.disconnect()


def test_socket_wrong_auth_falls_through_to_query():
    client = _socket(auth={"token": "wrong"}, query_string=f"token={TOKEN}")
    assert client.is_connected()
    client.disconnect()


def test_socket_wrong_query_shadows_good_cookie():
    client = _socket(
        query_string="token=wrong",
        headers={"Cookie": f"quietroom_token={TOKEN}"},
    )
    assert not client.is_connected()


def test_socket_forwarded_for_header_changes_nothing():
    assert not _socket(headers={"X-Forwarded-For": "1.2.3.4"}).is_connected()
