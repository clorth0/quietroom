"""THROWAWAY PROTOTYPE. Not imported by quietroom. Do not copy into the package.

Answers five design questions for AGENTS-PLAN.md by running the current
package and by parsing older revisions with git show. Delete this file when
the real changes land.
"""
from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def git_show(rev: str, path: str) -> str:
    out = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


class _EmptyRadio:
    def capture_iq(self, center_hz: int, sample_rate: int, n_samples: int) -> np.ndarray:
        return np.zeros(0, dtype=np.complex64)


class _ShortRadio:
    def capture_iq(self, center_hz: int, sample_rate: int, n_samples: int) -> np.ndarray:
        return np.zeros(8, dtype=np.complex64)


def measure_false_clean() -> dict:
    from quietroom.audio.capture import audio_correlation_test
    from quietroom.cli import format_verdict
    from quietroom.radio import hackrf as hackrf_mod
    from quietroom.radio.hackrf import HackRFDevice

    empty = audio_correlation_test(
        _EmptyRadio(), 308_000_000, duration_s=0.05, sample_rate=8_000,
        play_record=lambda sig, fs: np.asarray(sig, dtype=float),
    )
    short = audio_correlation_test(
        _ShortRadio(), 308_000_000, duration_s=0.05, sample_rate=8_000,
        play_record=lambda sig, fs: np.asarray(sig, dtype=float),
    )

    class _DeadProc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")
            self.returncode = 1

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 1

    real_popen = hackrf_mod.subprocess.Popen

    def _popen(*_a, **_k):
        return _DeadProc()

    hackrf_mod.subprocess.Popen = _popen
    try:
        samples = HackRFDevice().capture_iq(308_000_000, 2_000_000, 1000)
        raised = False
    except Exception as exc:  # noqa: BLE001 - the measurement is whether it raises
        samples = np.zeros(0, dtype=np.complex64)
        raised = True
        raise_type = type(exc).__name__
    else:
        raise_type = None
    finally:
        hackrf_mod.subprocess.Popen = real_popen

    return {
        "empty_iq_correlation": empty,
        "empty_iq_verdict": format_verdict(308_000_000, empty),
        "short_iq_correlation": short,
        "short_iq_verdict": format_verdict(308_000_000, short),
        "dead_process_raised": raised,
        "dead_process_raise_type": raise_type,
        "dead_process_sample_count": int(len(samples)),
        "clean_phrase": "not a detectable live audio bug",
    }


@dataclass(frozen=True)
class IqCapture:
    samples: np.ndarray
    expected: int

    def __post_init__(self) -> None:
        n = len(self.samples)
        if n != self.expected or n == 0:
            raise ValueError(
                f"capture has {n} samples, expected {self.expected}. "
                "An empty capture is not a correlation."
            )


@dataclass(frozen=True)
class NoAudioMatch:
    correlation: float


@dataclass(frozen=True)
class CaptureFailed:
    reason: str


def _try_capture(samples: np.ndarray, expected: int) -> str:
    try:
        IqCapture(samples, expected)
    except ValueError as exc:
        return CaptureFailed(str(exc)).reason
    return NoAudioMatch(0.0).__class__.__name__


def measure_verdict_shape() -> dict:
    return {
        "empty_result": _try_capture(np.zeros(0, dtype=np.complex64), 1000),
        "short_result": _try_capture(np.zeros(8, dtype=np.complex64), 1000),
        "full_result": _try_capture(np.zeros(1000, dtype=np.complex64), 1000),
        "float_zero_still_prints_clean": True,
    }


@dataclass(frozen=True)
class SweepPlan:
    start_hz: int
    stop_hz: int
    bin_hz: int
    mode: str

    def __post_init__(self) -> None:
        from quietroom.engine.detectors.signatures import NARROWBAND_HZ
        from quietroom.radio.hackrf import MAX_HZ, MIN_HZ

        if self.mode not in {"search", "overview"}:
            raise ValueError("mode must be search or overview")
        if not (MIN_HZ <= self.start_hz < self.stop_hz <= MAX_HZ):
            raise ValueError("range outside HackRF limits")
        if self.mode == "search" and self.bin_hz > NARROWBAND_HZ:
            raise ValueError(
                f"search bin {self.bin_hz} Hz is wider than the narrowband "
                f"threshold {NARROWBAND_HZ} Hz. Pass mode='overview', or pick "
                f"a finer bin."
            )


def _plan(bin_hz: int, mode: str) -> str:
    try:
        SweepPlan(300_000_000, 320_000_000, bin_hz, mode)
    except ValueError as exc:
        return f"rejected: {exc}"
    return "accepted"


def _numeric_defaults(source: str, filename: str) -> list[tuple[str, str, int]]:
    """Return (filename, name, value) for numeric defaults and assignments."""
    tree = ast.parse(source)
    found: list[tuple[str, str, int]] = []

    def add(name: str, node: ast.AST | None) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value >= 1000:
            found.append((filename, name, node.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            add(node.target.id, node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    add(target.id, node.value)
                elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    for elt, value in zip(target.elts, node.value.elts, strict=False):
                        if isinstance(elt, ast.Name):
                            add(elt.id, value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = [a.arg for a in args.args]
            defaults = args.defaults
            # positional defaults align to the end of the positional args
            offset = len(names) - len(defaults)
            for index, default in enumerate(defaults):
                add(f"{node.name}.{names[offset + index]}", default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
                add(f"{node.name}.{arg.arg}", default)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else ""
            if name != "add_argument":
                continue
            dest = None
            default_node = None
            for kw in node.keywords:
                if kw.arg == "default":
                    default_node = kw.value
                if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                    dest = str(kw.value.value)
            if dest is None:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        flag = arg.value
                        if flag.startswith("--"):
                            dest = flag.lstrip("-").replace("-", "_")
            if dest is not None:
                add(dest, default_node)
    return found


def _duplicates(rows: list[tuple[str, str, int]]) -> list[dict]:
    by_value: dict[int, list[tuple[str, str]]] = {}
    for filename, name, value in rows:
        by_value.setdefault(value, []).append((filename, name))
    out = []
    for value, places in sorted(by_value.items()):
        files = {filename for filename, _name in places}
        if len(files) < 2:
            continue
        out.append({
            "value": value,
            "places": [f"{filename}:{name}" for filename, name in places],
        })
    return out


def measure_policy() -> dict:
    rows_head: list[tuple[str, str, int]] = []
    for path in (ROOT / "quietroom").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        rows_head.extend(_numeric_defaults(path.read_text(), rel))

    old_rows: list[tuple[str, str, int]] = []
    for path in (
        "quietroom/cli.py",
        "quietroom/web/session.py",
        "quietroom/radio/hackrf.py",
        "quietroom/engine/detectors/signatures.py",
    ):
        old_rows.extend(_numeric_defaults(git_show("47fde9b^", path), path))

    from quietroom.radio.recorded import demo_device

    requested = 25_000
    spec = next(iter(demo_device().sweep(300_000_000, 320_000_000, requested, cycles=1)))
    actual_bin = float(spec.freqs_hz[1] - spec.freqs_hz[0])
    return {
        "demo_requested_bin_hz": requested,
        "demo_actual_bin_hz": actual_bin,
        "old_default_bin_1mhz_search": _plan(1_000_000, "search"),
        "current_default_bin_25khz_search": _plan(25_000, "search"),
        "overview_bin_1mhz": _plan(1_000_000, "overview"),
        "duplicated_literals_at_head": _duplicates(rows_head),
        "duplicated_literals_before_47fde9b": _duplicates(old_rows),
    }


def measure_score_holes() -> dict:
    from quietroom.engine.persistence import _annotate
    from quietroom.engine.score import squash
    from quietroom.engine.spectrum import Candidate, Finding
    from quietroom.scan import confirmed_findings

    candidate = Candidate(308e6, 25e3, -30.0, 20.0, bin_hz=25e3, n_bins=1)
    raw = 102.0
    forgotten = Finding(
        candidate=candidate,
        score=squash(raw),
        band_label="unknown",
        raw_score=None,
    )
    annotated = _annotate(forgotten, hits=3, n=3, min_hits=2)
    pinned = Finding(candidate=candidate, score=100.0, band_label="unknown", raw_score=None)
    pinned_out = _annotate(pinned, hits=3, n=3, min_hits=2)

    from quietroom.engine.baseline import build_baseline
    from quietroom.engine.spectrum import PowerSpectrum

    freqs = np.linspace(300e6, 320e6, 21)
    quiet = np.full(freqs.shape, -90.0)
    hot = quiet.copy()
    hot[10] = -20.0
    baseline = build_baseline([
        PowerSpectrum(freqs, quiet, 0.0),
        PowerSpectrum(freqs, quiet, 1.0),
        PowerSpectrum(freqs, quiet, 2.0),
    ])
    live = PowerSpectrum(freqs, hot, 3.0)

    class _ShortSweep:
        def sweep(self, f_start_hz, f_stop_hz, bin_hz, cycles=1):
            yield live

    findings = confirmed_findings(
        _ShortSweep(), baseline, n_sweeps=3, min_hits=2,
        f_start_hz=300_000_000, f_stop_hz=320_000_000, bin_hz=1_000_000,
    )
    return {
        "forgotten_raw_annotated_raw": annotated.raw_score,
        "forgotten_raw_true_raw_plus_bonus": raw + 10.0,
        "forgotten_raw_delta": (annotated.raw_score or 0) - (raw + 10.0),
        "pinned_100_annotated_raw": pinned_out.raw_score,
        "pinned_100_annotated_score": pinned_out.score,
        "short_sweep_still_reports": len(findings),
        "short_sweep_min_hits_requested": 2,
    }


def _handlers(source: str) -> list[dict]:
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if attr != "on":
                continue
            event = ""
            if deco.args and isinstance(deco.args[0], ast.Constant):
                event = str(deco.args[0].value)
            body_names = []
            for stmt in node.body:
                if isinstance(stmt, ast.Return):
                    body_names.append("return")
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                    call = stmt.value.func
                    body_names.append(call.id if isinstance(call, ast.Name) else "call")
                else:
                    body_names.append(type(stmt).__name__)
            delegated = body_names == ["dispatch"]
            found.append({
                "event": event,
                "function": node.name,
                "delegates_to_dispatch": delegated,
                "body": body_names,
            })
    return found


def _cors_arg(source: str) -> list[str]:
    tree = ast.parse(source)
    kinds = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "SocketIO":
            continue
        for kw in node.keywords:
            if kw.arg != "cors_allowed_origins":
                continue
            value = kw.value
            if isinstance(value, ast.List):
                kinds.append("list")
            elif isinstance(value, ast.Name):
                kinds.append(f"name:{value.id}")
            else:
                kinds.append(type(value).__name__)
    return kinds


def measure_boundaries() -> dict:
    head = (ROOT / "quietroom/web/app.py").read_text()
    before_error_fix = git_show("2c4e21e^", "quietroom/web/app.py")
    before_cors_fix = git_show("bfacdd3^", "quietroom/web/app.py")
    return {
        "handlers_at_head": _handlers(head),
        "handlers_before_2c4e21e": _handlers(before_error_fix),
        "cors_at_head": _cors_arg(head),
        "cors_before_bfacdd3": _cors_arg(before_cors_fix),
        "engineio_empty_list_disables_check": True,
        "engineio_file": ".venv/lib/python3.12/site-packages/engineio/server.py",
        "engineio_line": 202,
    }


def main() -> int:
    report = {
        "false_clean": measure_false_clean(),
        "verdict_shape": measure_verdict_shape(),
        "policy": measure_policy(),
        "score_holes": measure_score_holes(),
        "boundaries": measure_boundaries(),
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
