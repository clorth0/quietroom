"""quietroom-node --rebaseline / --ack / --promote-anchor: queue, validate, wait."""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quietroom.node import cli
from quietroom.store import Store

CONFIG = """
[node]
id = "n1"
[store]
path = "{db}"
[[segments]]
name = "uhf"
start_hz = 300_000_000
stop_hz = 301_000_000
[[segments]]
name = "off"
start_hz = 400_000_000
stop_hz = 401_000_000
enabled = false
"""


@pytest.fixture
def conf(tmp_path):
    p = tmp_path / "node.toml"
    p.write_text(CONFIG.format(db=tmp_path / "q.db"))
    return p


def rows(conf):
    db = sqlite3.connect(conf.parent / "q.db")
    return db.execute("SELECT node_id,segment,command,args,status FROM control_requests").fetchall()


def test_rebaseline_is_queued_for_the_running_node(conf, capsys):
    assert cli.main(["--config", str(conf), "--rebaseline", "uhf", "--note", "desk moved"]) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["status"] == "pending" and out["command"] == "rebaseline"
    [(node, seg, cmd, args, status)] = rows(conf)
    assert (node, seg, cmd, status) == ("n1", "uhf", "rebaseline", "pending")
    assert json.loads(args) == {"force": False, "note": "desk moved"}


def test_force_is_recorded_and_warns_loudly(conf, capsys):
    assert cli.main(["--config", str(conf), "--rebaseline", "uhf", "--force"]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "incident" in err and "anchor" in err
    assert json.loads(rows(conf)[0][3])["force"] is True


def test_ack_parses_frequency_and_promote_anchor_queues(conf, capsys):
    assert cli.main(["--config", str(conf), "--ack", "uhf", "--freq", "300.2M"]) == 0
    assert cli.main(["--config", str(conf), "--promote-anchor", "uhf"]) == 0
    (_, _, c1, a1, _), (_, _, c2, _, _) = rows(conf)
    assert c1 == "ack" and json.loads(a1)["freq_hz"] == 300_200_000
    assert c2 == "promote-anchor"


@pytest.mark.parametrize(
    "argv, msg",
    [
        (["--rebaseline", "nope"], "unknown segment"),
        (["--rebaseline", "off"], "disabled"),
        (["--ack", "uhf", "--freq", "lots"], "invalid frequency"),
    ],
)
def test_bad_requests_are_rejected_before_queueing(conf, argv, msg):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(conf), *argv])
    assert msg in str(e.value)
    assert not (conf.parent / "q.db").exists() or rows(conf) == []


@pytest.mark.parametrize(
    "argv", [["--force"], ["--freq", "300M"], ["--rebaseline", "uhf", "--freq", "1M"],
             ["--ack", "uhf", "--force"]],
)
def test_flags_only_apply_to_their_command(conf, argv):
    with pytest.raises(SystemExit):
        cli.main(["--config", str(conf), *argv])


def test_control_commands_need_a_store(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text('[[segments]]\nname = "uhf"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(p), "--rebaseline", "uhf"])
    assert "[store] path" in str(e.value)


def _finish_soon(db, status, result):
    def run():
        s = Store(db)  # one connection, like the daemon, closed when done
        try:
            for _ in range(100):
                pending = s.pending_controls("n1")
                if pending:
                    s.finish_control(pending[0]["id"], status, result)
                    return
                time.sleep(0.02)
        finally:
            s.close()

    t = threading.Thread(target=run)
    t.start()
    return t


@pytest.mark.parametrize("status, code", [("done", 0), ("refused", 2), ("failed", 1)])
def test_wait_reports_the_outcome(conf, capsys, status, code):
    t = _finish_soon(conf.parent / "q.db", status, {"why": "x"})
    rc = cli.main(["--config", str(conf), "--rebaseline", "uhf", "--wait", "5"])
    t.join()
    assert rc == code
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == status


def test_wait_times_out_when_no_node_is_running(conf, capsys):
    assert cli.main(["--config", str(conf), "--rebaseline", "uhf", "--wait", "0.2"]) == 3
    assert "still pending" in capsys.readouterr().err


def test_daemon_mode_does_not_buffer_every_event_forever(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "MonitorService", Fake)
    assert cli.main(["--demo"]) == 0
    assert seen["sink"] is None  # was events.append: unbounded growth in a daemon


def _node_cmd():
    exe = Path(sys.executable).with_name("quietroom-node")
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "quietroom.node.cli"]


def test_demo_node_once_subprocess_reports_the_planted_308mhz_finding():
    """The installed entry point, as CI and operators run it, with the default config."""
    p = subprocess.run(
        [*_node_cmd(), "--demo", "--once"], capture_output=True, text=True, timeout=120
    )
    assert p.returncode == 0, p.stderr
    lines = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
    sweeps = {e["segment"]: e for e in lines if e.get("type") == "sweep"}
    assert set(sweeps) == {"covert-uhf", "wifi24"}
    hits = [f for f in sweeps["covert-uhf"]["findings"] if abs(f["freq_hz"] - 308e6) < 50e3]
    assert hits and hits[0]["score"] >= 80, sweeps["covert-uhf"]["findings"]
    assert sweeps["wifi24"]["findings"] == []
    health = lines[-1]["health"]
    assert health["last_error"] is None and lines[-1]["tick"]["errors"] == []
