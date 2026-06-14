from quietroom.web.app import create_app


def test_index_has_canvas_and_table_and_local_socketio():
    app, _ = create_app(demo=True)
    body = app.test_client().get("/").get_data(as_text=True)
    assert "<canvas" in body
    assert 'id="suspects"' in body
    # Socket.IO client is vendored locally (offline-first), not a CDN.
    assert "socket.io.min.js" in body
    assert "cdn." not in body


def test_static_assets_served():
    app, _ = create_app(demo=True)
    client = app.test_client()
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/socket.io.min.js").status_code == 200
