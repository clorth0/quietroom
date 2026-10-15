from quietroom.web.app import create_app, emit_tick


def test_emit_tick_emits_sweep_and_findings():
    app, socketio = create_app(demo=True)
    app.scan_session.capture_baseline(cycles=3)

    sent = []
    emit_tick(app.scan_session, lambda name, payload: sent.append((name, payload)))

    names = [n for n, _ in sent]
    assert "sweep" in names and "findings" in names
    findings_payload = next(p for n, p in sent if n == "findings")
    assert findings_payload["findings"][0]["score"] > 50.0


def test_start_then_stop_toggles_running_flag():
    app, socketio = create_app(demo=True)
    client = socketio.test_client(app)
    client.emit("start", {})
    assert app.config["RUNNING"] is True
    client.emit("stop", {})
    assert app.config["RUNNING"] is False


def test_safe_tick_reports_device_errors_instead_of_dying():
    from quietroom.web.app import safe_tick

    class Boom:
        def sweep_once(self):
            raise RuntimeError("hackrf_sweep exited: Resource busy")

    sent = []
    ok = safe_tick(Boom(), lambda n, p: sent.append((n, p)))
    assert ok is False
    assert sent == [("error", {"message": "sweep failed: hackrf_sweep exited: Resource busy"})]


def test_safe_tick_returns_true_on_success():
    from quietroom.web.app import safe_tick
    app, socketio = create_app(demo=True)
    app.scan_session.capture_baseline(cycles=3)
    assert safe_tick(app.scan_session, lambda n, p: None) is True
