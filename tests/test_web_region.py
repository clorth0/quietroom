import pytest

from quietroom.bands import EU_BANDS, US_BANDS
from quietroom.radio.recorded import streaming_demo_device
from quietroom.web import app as webapp
from quietroom.web.app import create_app
from quietroom.web.session import ScanSession


def test_session_region_defaults_to_us_and_switches():
    s = ScanSession(streaming_demo_device())
    assert s.region == "us" and s.bands is US_BANDS
    s.set_region("eu")
    assert s.region == "eu" and s.bands is EU_BANDS


def test_session_rejects_unknown_region():
    with pytest.raises(ValueError):
        ScanSession(streaming_demo_device()).set_region("mars")


def test_session_uses_region_bands_for_findings():
    # 868.3 MHz: US cellular 850 (shared), EU SRD/ISM 868 (shared, different label).
    from quietroom.engine.spectrum import Candidate
    s = ScanSession(streaming_demo_device(), region="eu")
    c = Candidate(868.3e6, 10e3, -70.0, 20.0)
    assert "868" in s.score_label(c)
    s.set_region("us")
    assert "850" in s.score_label(c)


def test_set_region_socket_event_and_validation():
    app, socketio = create_app(demo=True)
    client = socketio.test_client(app)
    client.emit("set_region", {"region": "eu"})
    got = {e["name"]: e["args"][0] for e in client.get_received()}
    assert got["region"] == {"region": "eu"}
    assert app.scan_session.region == "eu"
    client.emit("set_region", {"region": "mars"})
    got = {e["name"]: e["args"][0] for e in client.get_received()}
    assert "error" in got and app.scan_session.region == "eu"


def test_create_app_region_and_cli_flag(monkeypatch):
    app, _ = create_app(demo=True, region="eu")
    assert app.scan_session.region == "eu"
    seen = {}
    monkeypatch.setattr(
        webapp, "_run",
        lambda socketio, app, host, port: seen.update(r=app.scan_session.region),
    )
    assert webapp.main(["--demo", "--region", "eu"]) == 0
    assert seen["r"] == "eu"


def test_index_has_region_selector():
    app, _ = create_app(demo=True, region="eu")
    body = app.test_client().get("/").get_data(as_text=True)
    assert 'id="region"' in body and 'value="eu" selected' in body
