import subprocess
import sys
import tomllib
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_sounddevice_is_an_optional_audio_extra():
    p = _pyproject()
    core = " ".join(p["project"]["dependencies"])
    assert "sounddevice" not in core
    assert any("sounddevice" in d for d in p["project"]["optional-dependencies"]["audio"])


def test_dev_dependency_group_has_test_and_lint_tools():
    group = " ".join(_pyproject()["dependency-groups"]["dev"])
    for tool in ("pytest", "ruff", "mypy"):
        assert tool in group


def test_missing_sounddevice_gives_actionable_error(monkeypatch):
    from quietroom.audio import capture
    monkeypatch.setitem(sys.modules, "sounddevice", None)   # import fails
    with pytest.raises(RuntimeError, match="quietroom\\[audio\\]"):
        capture.play_and_record(np.zeros(10, dtype=np.float32), 8_000)


def test_demo_cli_runs_end_to_end_as_a_subprocess():
    out = subprocess.run([sys.executable, "-m", "quietroom.cli", "--demo"],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "308.000" in out.stdout
