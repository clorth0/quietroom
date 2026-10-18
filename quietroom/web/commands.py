"""One parser for every socket event the web UI sends."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, assert_never

from quietroom.bands import REGIONS
from quietroom.engine.plan import parse_freq_hz
from quietroom.radio.hackrf import MAX_HZ, MIN_HZ
from quietroom.web.token import request_token_accepted


@dataclass(frozen=True)
class Connect:
    auth: object


@dataclass(frozen=True)
class SetRegion:
    region: str


@dataclass(frozen=True)
class SetSweep:
    start_hz: int
    stop_hz: int
    bin_hz: int


@dataclass(frozen=True)
class CaptureBaseline:
    cycles: int


@dataclass(frozen=True)
class Investigate:
    freq_hz: float


@dataclass(frozen=True)
class Start:
    pass


@dataclass(frozen=True)
class Stop:
    pass


Command = Connect | SetRegion | SetSweep | CaptureBaseline | Investigate | Start | Stop


@dataclass
class CommandContext:
    app: Any
    token: str | None
    socketio: Any
    emit: Callable[[str, dict], None]


def parse_command(event: str, data: object) -> Command | str:
    """Return a command, or an error string the UI can show."""
    if event == "connect":
        return Connect(data)
    if event == "stop":
        return Stop()
    if event == "start":
        return Start()
    payload = data if isinstance(data, dict) else {}
    if event == "set_region":
        region = str(payload.get("region", "")).lower()
        if region not in REGIONS:
            return f"unknown region; choose from {sorted(REGIONS)}"
        return SetRegion(region)
    if event == "set_sweep":
        fields = []
        for key in ("start", "stop", "bin"):
            value = _plan_frequency(payload.get(key))
            if isinstance(value, str):
                return value
            fields.append(value)
        return SetSweep(*fields)
    if event == "capture_baseline":
        cycles = _whole_number(payload.get("cycles", 5))
        if cycles is None or not 1 <= cycles <= 30:
            return "invalid cycles"
        return CaptureBaseline(cycles)
    if event == "investigate":
        freq = _frequency(payload.get("freq_hz"))
        if isinstance(freq, str):
            return freq
        return Investigate(freq)
    return f"unknown event {event}"


def dispatch(event: str, data: object, ctx: CommandContext) -> object:
    """Parse `event` and emit the matching socket payload."""
    parsed = parse_command(event, data)
    if isinstance(parsed, str):
        ctx.emit("error", {"message": parsed})
        return None
    return _apply(parsed, ctx)


def _apply(cmd: Command, ctx: CommandContext) -> object:
    if isinstance(cmd, Connect):
        return _connect_allowed(cmd.auth, ctx.token)
    if isinstance(cmd, Stop):
        ctx.app.config["RUNNING"] = False
        return None
    session = ctx.app.scan_session
    if session is None:
        ctx.emit("error", {"message": "no device"})
        return None
    if isinstance(cmd, Start):
        if ctx.app.config["RUNNING"]:
            return None
        ctx.app.config["RUNNING"] = True
        ctx.socketio.start_background_task(_sweep_loop, ctx)
        return None
    if isinstance(cmd, SetSweep):
        try:
            plan = session.set_plan(cmd.start_hz, cmd.stop_hz, cmd.bin_hz)
        except ValueError as exc:
            ctx.emit("error", {"message": str(exc)})
            return None
        ctx.emit("sweep_plan", plan)
        return None
    if isinstance(cmd, SetRegion):
        session.set_region(cmd.region)
        ctx.emit("region", {"region": session.region})
        return None
    if isinstance(cmd, CaptureBaseline):
        try:
            count = session.capture_baseline(cycles=cmd.cycles)
        except Exception as exc:  # noqa: BLE001 - device failure is an error event
            ctx.emit("error", {"message": f"baseline failed: {exc}"})
            return None
        ctx.emit("baseline_ready", {"sweep_count": count})
        return None
    if isinstance(cmd, Investigate):
        try:
            payload = session.investigate(cmd.freq_hz, **_audio_kwargs(ctx.app))
        except KeyError:
            ctx.emit("error", {"message": "no current suspect at that frequency"})
            return None
        except Exception as exc:  # noqa: BLE001 - device/audio failure is an error event
            ctx.emit("error", {"message": f"investigate failed: {exc}"})
            return None
        ctx.emit("finding", payload)
        return None
    assert_never(cmd)


def _sweep_loop(ctx: CommandContext) -> None:
    from quietroom.web.app import safe_tick

    while ctx.app.config["RUNNING"]:
        if not safe_tick(ctx.app.scan_session, ctx.socketio.emit):
            ctx.app.config["RUNNING"] = False
            break
        ctx.socketio.sleep(0.5)


def _audio_kwargs(app: Any) -> dict:
    if app.config.get("DEMO"):
        return {"play_record": lambda sig, fs: sig.astype(float)}
    return {}


def _connect_allowed(auth: object, token: str | None) -> bool:
    """Socket policy: the shared acceptance, plus the auth-dict presentation."""
    given = auth.get("token") if isinstance(auth, dict) else None
    return request_token_accepted(token, auth_token=given)


def _plan_frequency(raw: object) -> int | str:
    """One start/stop/bin field: Hz number, or CLI syntax via parse_freq_hz."""
    if isinstance(raw, bool):
        return "invalid frequency"
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return parse_freq_hz(raw)
        except ValueError as exc:
            return str(exc)
    return "invalid frequency"


def _whole_number(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _frequency(raw: object) -> float | str:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return "invalid frequency"
    try:
        freq = float(raw)
    except ValueError:
        return "invalid frequency"
    if not MIN_HZ <= freq <= MAX_HZ:
        return "frequency out of range"
    return freq
