from quietroom.web import app as webapp


def test_build_server_demo_returns_app_and_socketio():
    # main() would call socketio.run (blocking); test the builder it wraps instead.
    app, socketio = webapp.build_server(demo=True, host="127.0.0.1", port=8770)
    assert app.config["DEMO"] is True
    assert app.scan_session is not None


def test_main_is_callable():
    assert callable(webapp.main)
