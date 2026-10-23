"""Invalid configs fail loudly where operators meet them: at daemon start and in --check-config.

  AC9  quietroom-node --config bad.toml exits 2 with every problem named, no traceback
  AC10 --check-config on a bad config: exit 1, report ok=false listing each key
  AC11 warnings (plain http webhook, poll_s slower than a segment) reach --check-config and startup
  AC12 the shipped example config (as shipped and fully uncommented) and the demo config validate
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from quietroom.config import ConfigError, NodeConfig, config_warnings, default_config, load_config
from quietroom.node import cli

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.toml"

BAD = """
[node]
id = "n1"
poll_s = 0
[[segments]]
name = "uhf"
start_hz = 300_000_000
stop_hz = 301_000_000
priority = "high"
[alerts]
min_scor = 80
webhook_url = "file:///etc/passwd"
"""


def _write(tmp_path, text, name="node.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_daemon_start_fails_loudly_naming_every_bad_key(tmp_path):
    p = _write(tmp_path, BAD)
    r = subprocess.run(
        [sys.executable, "-m", "quietroom.node.cli", "--config", str(p)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert str(p) in r.stderr
    for key in ("node.poll_s", "segments[0].priority", "alerts.min_scor", "alerts.webhook_url"):
        assert key in r.stderr, key
    assert "did you mean 'min_score'" in r.stderr


def test_main_returns_2_for_an_invalid_config(tmp_path, capsys):
    p = _write(tmp_path, BAD)
    assert cli.main(["--config", str(p)]) == 2
    err = capsys.readouterr().err
    assert "invalid config" in err and "node.poll_s" in err


def test_toml_syntax_errors_are_reported_the_same_way(tmp_path, capsys):
    p = _write(tmp_path, "[node]\nid = \n")
    assert cli.main(["--config", str(p)]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_check_config_reports_every_problem_and_exits_1(tmp_path, capsys):
    p = _write(tmp_path, BAD)
    assert cli.main(["--config", str(p), "--check-config"]) == 1
    cap = capsys.readouterr()
    report = json.loads(cap.out)
    assert report["ok"] is False
    keys = sorted(e["key"] for e in report["config_errors"])
    assert keys == sorted(
        ["node.poll_s", "segments[0].priority", "alerts.min_scor", "alerts.webhook_url"]
    )
    assert len(report["errors"]) == 4
    assert cap.err.count("ERROR: ") == 4


def test_check_config_warns_on_plain_http_webhook_and_slow_poll(tmp_path, capsys):
    text = (
        '[node]\nid = "n1"\npoll_s = 30\n'
        '[[segments]]\nname = "uhf"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n'
        "interval_s = 10\n"
        '[alerts]\nwebhook_url = "http://10.0.0.5/hook"\n'
    )
    p = _write(tmp_path, text)
    rc = cli.main(["--config", str(p), "--check-config"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0 and report["ok"] is True  # warnings do not fail the check
    joined = " ".join(report["warnings"])
    assert "alerts.webhook_url" in joined and "https" in joined
    assert "poll_s" in joined and "uhf" in joined


def test_the_daemon_prints_config_warnings_at_startup(tmp_path, capsys, monkeypatch):
    p = _write(
        tmp_path,
        '[node]\nid = "n1"\n[[segments]]\nname = "uhf"\nstart_hz = 300_000_000\n'
        'stop_hz = 301_000_000\n[alerts]\nwebhook_url = "http://10.0.0.5/hook"\n',
    )

    class Fake:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            return 0

    monkeypatch.setattr(cli, "MonitorService", Fake)
    cli.main(["--config", str(p), "--demo"])
    assert "WARNING: alerts.webhook_url" in capsys.readouterr().err


def test_poll_slower_than_the_fastest_segment_is_a_warning():
    cfg = NodeConfig.from_dict(
        {
            "node": {"poll_s": 30},
            "segments": [
                {"name": "a", "start_hz": 300_000_000, "stop_hz": 301_000_000, "interval_s": 10},
                {"name": "b", "start_hz": 400_000_000, "stop_hz": 401_000_000,
                 "interval_s": 5, "enabled": False},
            ],
        }
    )
    [w] = config_warnings(cfg)
    assert "'a'" in w and "'b'" not in w  # disabled segments never run, so never late


# --- AC12: shipped and demo configs ----------------------------------------------------


def test_the_shipped_example_config_validates():
    cfg = load_config(EXAMPLE)
    assert [s.name for s in cfg.segments] == ["covert-uhf", "wifi24", "overview"]
    assert config_warnings(cfg) == []


def _uncommented_example(tmp_path):
    """config.example.toml with every commented-out `# key = value` / `# [table]` enabled."""
    out = []
    for line in EXAMPLE.read_text().splitlines():
        m = re.match(r"^#\s?((\[[\w.]+\])|([a-z_]+\s*=.*))$", line)
        out.append(m.group(1) if m else line)
    text = "\n".join(out).replace('base_url = ""', 'base_url = "https://central.example"')
    return _write(tmp_path, text, "example-all.toml")


def test_every_commented_option_in_the_example_is_a_valid_key(tmp_path):
    cfg = load_config(_uncommented_example(tmp_path))
    assert cfg.email is not None and cfg.email.host == "smtp.example.com"
    assert cfg.fleet is not None and cfg.fleet.cert_warn_days == 30
    assert cfg.rebaseline.freeze_score == 80


def test_the_example_documents_the_new_fleet_outbox_keys():
    text = EXAMPLE.read_text()
    for k in ("outbox_max_rows", "outbox_max_age_s", "outbox_max_attempts"):
        assert k in text


def test_the_demo_config_is_built_through_the_validator(monkeypatch):
    calls = []
    real = NodeConfig.from_dict.__func__

    def spy(cls, d):
        calls.append(d)
        return real(cls, d)

    monkeypatch.setattr(NodeConfig, "from_dict", classmethod(spy))
    cfg = default_config()
    assert calls, "default_config() must go through NodeConfig.from_dict validation"
    assert cfg.node_id == "demo-node"
    assert [(s.name, s.interval_s, s.priority) for s in cfg.segments] == [
        ("covert-uhf", 60, 10),
        ("wifi24", 300, 5),
    ]


def test_demo_check_config_passes(capsys):
    assert cli.main(["--demo", "--check-config"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_config_error_names_the_file(tmp_path):
    p = _write(tmp_path, BAD)
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert str(p) in str(ei.value)


def test_check_config_legacy_token_warning_names_the_removal_plan(tmp_path, capsys):
    p = _write(
        tmp_path,
        '[node]\nid = "n1"\n[[segments]]\nname = "uhf"\nstart_hz = 300_000_000\n'
        'stop_hz = 301_000_000\n[fleet]\nbase_url = "https://c.test"\nauth = "legacy-token"\n',
    )
    from quietroom.node.checkconfig import check_node_config

    report = check_node_config(load_config(p), {"QUIETROOM_CENTRAL_TOKEN": "x" * 40})
    [w] = [w for w in report["warnings"] if "legacy" in w]
    assert "0.5.0" in w and "2027-04-01" in w
    assert report["config_errors"] == []


def test_a_missing_config_file_is_a_clean_error(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml")]) == 2
    err = capsys.readouterr().err
    assert "cannot read config" in err and "Traceback" not in err


def test_systemd_does_not_restart_loop_on_a_config_error():
    # Review F3: exit 2 (bad config / usage) is permanent; restarting every 10 s
    # only floods the journal.
    unit = (ROOT / "packaging/systemd/quietroom-node.service").read_text()
    assert re.search(r"^RestartPreventExitStatus=.*\b2\b", unit, re.M)


def test_legacy_token_over_plain_http_is_a_warning():
    # Review F4: the shared bearer token would cross the network in clear.
    cfg = NodeConfig.from_dict(
        {
            "segments": [{"name": "a", "start_hz": 300_000_000, "stop_hz": 301_000_000}],
            "fleet": {"base_url": "http://central.lan", "auth": "legacy-token"},
        }
    )
    [w] = config_warnings(cfg)
    assert "fleet.base_url" in w and "QUIETROOM_CENTRAL_TOKEN" in w and "https" in w


def test_segments_given_as_a_table_says_array_of_tables():
    with pytest.raises(ConfigError, match=r"\[\[segments\]\]") as ei:
        NodeConfig.from_dict({"segments": {"name": "a"}})
    assert "array" in str(ei.value)
