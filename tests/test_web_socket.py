from quietroom.web.app import create_app


def _client():
    app, socketio = create_app(demo=True)
    return app, socketio, socketio.test_client(app)


def test_capture_baseline_emits_ready():
    app, socketio, client = _client()
    client.emit("capture_baseline", {"cycles": 3})
    events = {e["name"] for e in client.get_received()}
    assert "baseline_ready" in events


def test_investigate_emits_finding_with_verdict():
    app, socketio, client = _client()
    app.scan_session.capture_baseline(cycles=3)
    app.scan_session.sweep_once()
    client.get_received()  # drain
    client.emit("investigate", {"freq_hz": 308_000_000})
    received = client.get_received()
    findings = [e for e in received if e["name"] == "finding"]
    assert findings
    assert findings[0]["args"][0]["score"] >= 100.0


def test_investigate_out_of_range_emits_error():
    app, socketio, client = _client()
    client.emit("investigate", {"freq_hz": 99_000_000_000})  # above 6 GHz
    errors = [e for e in client.get_received() if e["name"] == "error"]
    assert errors
