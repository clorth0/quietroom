import ast
from pathlib import Path

import pytest

from quietroom.radio.device import Device, DeviceError, DeviceInfo
from quietroom.web.app import create_app
from quietroom.web.commands import parse_command


APP_PY = Path("quietroom/web/app.py")


def _errors(client):
    return [e["args"][0]["message"] for e in client.get_received() if e["name"] == "error"]


def _names(client):
    return [e["name"] for e in client.get_received()]


def _socketio_handlers(tree: ast.AST) -> list[ast.FunctionDef]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            call = deco if isinstance(deco, ast.Call) else None
            func = call.func if call is not None else None
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "on"
                and isinstance(func.value, ast.Name)
                and func.value.id == "socketio"
            ):
                found.append(node)
    return found


def _is_dispatch_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "dispatch"
    return isinstance(func, ast.Attribute) and func.attr == "dispatch"


def assert_handlers_delegate(source: str) -> None:
    """Each @socketio.on body is one call to quietroom.web.commands.dispatch."""
    handlers = _socketio_handlers(ast.parse(source))
    assert handlers, "expected @socketio.on handlers"
    for handler in handlers:
        stmt = handler.body[0] if len(handler.body) == 1 else None
        if isinstance(stmt, ast.Return):
            call = stmt.value
        elif isinstance(stmt, ast.Expr):
            call = stmt.value
        else:
            call = None
        if not _is_dispatch_call(call):
            raise AssertionError(
                f"{handler.name} must call quietroom.web.commands.dispatch and do nothing else"
            )


def test_socket_handlers_only_call_dispatch():
    assert_handlers_delegate(APP_PY.read_text())


def test_hand_rolled_handler_fails_the_ast_rule():
    source = '''
def create():
    @socketio.on("start")
    def _on_start(data):
        if session is None:
            return
        dispatch("start", data, ctx)
'''
    with pytest.raises(AssertionError, match="quietroom.web.commands.dispatch"):
        assert_handlers_delegate(source)


def test_cycles_outside_1_to_30_are_rejected():
    app, socketio = create_app(demo=True)
    client = socketio.test_client(app)
    client.emit("capture_baseline", {"cycles": "nope"})
    assert _errors(client) == ["invalid cycles"]
    assert "baseline_ready" not in _names(client)

    client.emit("capture_baseline", {"cycles": 0})
    received = client.get_received()
    messages = [e["args"][0]["message"] for e in received if e["name"] == "error"]
    names = [e["name"] for e in received]
    assert messages == ["invalid cycles"]
    assert "baseline_ready" not in names

    client.emit("capture_baseline", {"cycles": 31})
    assert _errors(client) == ["invalid cycles"]

    client.emit("capture_baseline", {"cycles": 1})
    assert "baseline_ready" in _names(client)


def test_invalid_frequency_and_out_of_range():
    app, socketio = create_app(demo=True)
    client = socketio.test_client(app)
    client.emit("investigate", {"freq_hz": "nope"})
    assert _errors(client) == ["invalid frequency"]
    client.emit("investigate", {"freq_hz": 1000})
    assert _errors(client) == ["frequency out of range"]


def test_unknown_region_names_the_known_regions():
    app, socketio = create_app(demo=True)
    client = socketio.test_client(app)
    client.emit("set_region", {"region": "zz"})
    msgs = _errors(client)
    assert msgs and "eu" in msgs[0] and "us" in msgs[0]


def test_start_with_no_session_emits_no_device():
    app, socketio = create_app(demo=True)
    app.scan_session = None
    client = socketio.test_client(app)
    client.emit("start", {})
    assert _errors(client) == ["no device"]
    assert app.config["RUNNING"] is False


def test_baseline_device_error_names_the_failure():
    class Broken(Device):
        def probe(self):
            return DeviceInfo("x", "x", "x")

        def sweep(self, *a, **k):
            raise DeviceError("hackrf_sweep exited (code 1): Resource busy")
            yield  # pragma: no cover

        def capture_iq(self, *a, **k):
            return None

    app, socketio = create_app(device=Broken())
    client = socketio.test_client(app)
    client.emit("capture_baseline", {"cycles": 3})
    msgs = _errors(client)
    assert msgs and msgs[0].startswith("baseline failed")
    assert "Resource busy" in msgs[0]


def test_parse_command_rejects_unknown_events_and_keeps_edges():
    assert parse_command("capture_baseline", {"cycles": 30}).cycles == 30
    assert parse_command("capture_baseline", {"cycles": 1}).cycles == 1
    assert parse_command("nope", {}) == "unknown event nope"
    freq = parse_command("investigate", {"freq_hz": 308_000_000})
    assert freq.freq_hz == 308_000_000
