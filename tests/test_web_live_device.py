from quietroom.radio.hackrf import HackRFDevice
from quietroom.web.app import create_app


def test_live_mode_wires_a_hackrf_device():
    # Non-demo create_app must wire a real HackRFDevice so the live web path has
    # a radio instead of None. Constructing HackRFDevice touches no hardware.
    app, _ = create_app(demo=False)
    assert app.scan_session is not None
    assert isinstance(app.scan_session.device, HackRFDevice)


def test_demo_mode_still_uses_demo_device():
    app, _ = create_app(demo=True)
    assert app.scan_session is not None
    assert not isinstance(app.scan_session.device, HackRFDevice)
