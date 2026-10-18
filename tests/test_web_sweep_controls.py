"""Web sweep span/step controls: CLI parity through the SweepPlan seam."""
import numpy as np
import pytest

from quietroom.engine.plan import SweepPlan
from quietroom.radio.device import Device, DeviceInfo
from quietroom.radio.recorded import spectrum_grid, streaming_demo_device
from quietroom.web.app import create_app
from quietroom.web.bind import LocalBind
from quietroom.web.commands import parse_command
from quietroom.web.session import ScanSession


class RecordingDevice(Device):
    def __init__(self):
        self.plans: list[SweepPlan] = []

    def probe(self):
        return DeviceInfo("rec", "RecordingDevice", "n/a")

    def sweep(self, plan, cycles=1):
        for _ in range(cycles):
            self.plans.append(plan)
            yield spectrum_grid(plan.start_hz, plan.stop_hz, plan.bin_hz, -95.0, 1.0)

    def capture_iq(self, center_hz, sample_rate, n_samples):
        return np.zeros(n_samples, dtype=np.complex64)


def _errors(client):
    return [e["args"][0]["message"] for e in client.get_received() if e["name"] == "error"]


def _events(client, name):
    return [e["args"][0] for e in client.get_received() if e["name"] == name]


def test_defaults_unchanged_on_session_and_page():
    s = ScanSession(streaming_demo_device())
    assert s.plan == SweepPlan.search()
    assert (s.plan.start_hz, s.plan.stop_hz, s.plan.bin_hz) == (
        300_000_000, 320_000_000, 25_000)
    app, _ = create_app(demo=True)
    body = app.test_client().get("/").get_data(as_text=True)
    assert 'id="sweep-start"' in body and 'value="300M"' in body
    assert 'id="sweep-stop"' in body and 'value="320M"' in body
    assert 'id="sweep-bin"' in body and 'value="25k"' in body


def test_parse_command_accepts_cli_frequency_syntax_and_hz_numbers():
    cmd = parse_command("set_sweep", {"start": "433.92M", "stop": "435M", "bin": "25k"})
    assert (cmd.start_hz, cmd.stop_hz, cmd.bin_hz) == (433_920_000, 435_000_000, 25_000)
    cmd = parse_command("set_sweep", {"start": 400_000_000, "stop": 420_000_000, "bin": 25_000})
    assert (cmd.start_hz, cmd.stop_hz, cmd.bin_hz) == (400_000_000, 420_000_000, 25_000)


def test_valid_custom_span_reaches_device_seam_as_exact_plan():
    dev = RecordingDevice()
    app, socketio = create_app(device=dev)
    client = socketio.test_client(app)
    client.emit("set_sweep", {"start": "400M", "stop": "420M", "bin": "25k"})
    assert _events(client, "sweep_plan") == [{
        "start_hz": 400_000_000, "stop_hz": 420_000_000, "bin_hz": 25_000,
        "mode": "search",
    }]
    assert app.scan_session.plan == SweepPlan.search(400_000_000, 420_000_000, 25_000)
    app.scan_session.sweep_once()
    assert dev.plans == [SweepPlan.search(400_000_000, 420_000_000, 25_000)]


def test_set_plan_resets_stale_baseline_and_preserves_overview_mode():
    s = ScanSession(streaming_demo_device(n_clean=3), overview=True,
                    bin_hz=1_000_000)
    s.capture_baseline(cycles=1)
    assert s.baseline is not None
    payload = s.set_plan(400_000_000, 500_000_000, 1_000_000)
    assert payload["mode"] == "overview"
    assert s.plan == SweepPlan.overview(400_000_000, 500_000_000, 1_000_000)
    assert s.baseline is None
    assert s.tracker.sweeps_seen == 0


@pytest.mark.parametrize("payload,fragment", [
    ({"start": "420M", "stop": "400M", "bin": "25k"}, "outside"),   # start >= stop
    ({"start": "400M", "stop": "420M", "bin": 0}, "bin_hz must be positive"),
    ({"start": "500k", "stop": "420M", "bin": "25k"}, "outside"),   # below 1 MHz
    ({"start": "400M", "stop": "7G", "bin": "25k"}, "outside"),     # above 6 GHz
    ({"start": "nope", "stop": "420M", "bin": "25k"}, "invalid frequency"),
    ({"start": "400M", "stop": "420M", "bin": "nope"}, "invalid frequency"),
    ({"start": "400M", "stop": "420M", "bin": "1M"}, "overview"),   # search bin too wide
    ({"start": "400M", "stop": "400.01M", "bin": "25k"}, "smaller than one bin"),
])
def test_invalid_sweep_rejected_without_sweeping_or_corrupting_state(payload, fragment):
    dev = RecordingDevice()
    app, socketio = create_app(device=dev)
    session = app.scan_session
    session.capture_baseline(cycles=1)
    spectrum_before, _ = session.sweep_once()
    plans_before = list(dev.plans)
    baseline_before = session.baseline
    plan_before = session.plan

    client = socketio.test_client(app)
    client.emit("set_sweep", payload)
    errors = _errors(client)
    assert errors and fragment in errors[0]
    assert _events(client, "sweep_plan") == []
    assert session.plan == plan_before
    assert session.baseline is baseline_before
    assert dev.plans == plans_before
    # Previous spectrum still describes the (unchanged) plan.
    spectrum_after, _ = session.sweep_once()
    assert spectrum_after["f0"] == spectrum_before["f0"]


def test_non_numeric_types_are_rejected():
    app, socketio = create_app(demo=True)
    client = socketio.test_client(app)
    client.emit("set_sweep", {"start": ["400M"], "stop": "420M", "bin": "25k"})
    errors = _errors(client)
    assert errors and "invalid frequency" in errors[0]
    assert app.scan_session.plan == SweepPlan.search()


def test_token_gate_still_guards_set_sweep():
    token = "s3cret-token-value"
    app, socketio = create_app(demo=True, bind=LocalBind(token=token))
    bad = socketio.test_client(app)
    assert not bad.is_connected()
    good = socketio.test_client(app, auth={"token": token})
    assert good.is_connected()
    good.emit("set_sweep", {"start": "400M", "stop": "420M", "bin": "25k"})
    assert _events(good, "sweep_plan")
    assert app.scan_session.plan.start_hz == 400_000_000


def test_frontend_controls_emit_set_sweep_with_cli_field_names():
    app, _ = create_app(demo=True)
    client = app.test_client()
    js = client.get("/static/app.js").get_data(as_text=True)
    assert 'emit("set_sweep"' in js
    for field in ("start", "stop", "bin"):
        assert field in js
