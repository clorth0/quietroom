"""SIGTERM / SIGINT stop the node daemon cleanly.

On a signal the daemon finishes the segment it is sweeping (a hardware sweep is
not interrupted half-way) and abandons the rest of the tick, tells systemd
STOPPING=1, flushes the alert and fleet outboxes once more, closes every SQLite
file and exits 0. A clean close is visible on disk: SQLite checkpoints the
store's write-ahead log and deletes the -wal/-shm files only when the last
connection closes properly; a killed process leaves them behind.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from quietroom.config import NodeConfig
from quietroom.node.service import MonitorService
from quietroom.node.stopping import Stopper
from quietroom.node.watchdog import SdNotifier

NODE = [sys.executable, "-m", "quietroom.node.cli"]


@pytest.fixture
def short_dir():
    # AF_UNIX paths are limited to ~107 bytes; pytest's tmp_path can be longer.
    d = tempfile.mkdtemp(prefix="qr-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _config(d: Path) -> Path:
    cfg = d / "node.toml"
    cfg.write_text(
        f"""
[node]
id = "shutdown-test"
poll_s = 0.5
[store]
path = "{d / 'node.db'}"
[alerts]
# Nothing listens here: alerts queue in the outbox and every flush fails fast.
webhook_url = "http://127.0.0.1:9/hook"
min_score = 50
[[segments]]
name = "covert-uhf"
start_hz = 300_000_000
stop_hz = 316_000_000
interval_s = 1.0
"""
    )
    return cfg


def _recv_until(sock: socket.socket, want: str, deadline: float) -> list[str]:
    got = []
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            got.append(sock.recv(4096).decode())
        except TimeoutError:
            break
        if any(want in m for m in got):
            break
    return got


def _no_lock_left(path: Path) -> None:
    db = sqlite3.connect(path, timeout=0)
    try:
        db.execute("BEGIN EXCLUSIVE")
        db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_real_daemon_stops_cleanly_on_signal(short_dir, sig):
    d = short_dir
    cfg = _config(d)
    sock_path = str(d / "notify")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(sock_path)
    env = {**os.environ, "NOTIFY_SOCKET": sock_path}
    proc = subprocess.Popen(
        [*NODE, "--config", str(cfg), "--demo"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        start = _recv_until(sock, "WATCHDOG=1", time.monotonic() + 30)
        assert "READY=1" in start and "WATCHDOG=1" in start, start  # running and ticking
        time.sleep(1.2)  # a couple more ticks: alerts are queued, the store is busy
        t0 = time.monotonic()
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=10)
        took = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    after = _recv_until(sock, "STOPPING=1", time.monotonic() + 1)
    sock.close()
    assert proc.returncode == 0, err
    assert took < 5, f"took {took:.1f} s to stop"
    assert "Traceback" not in err, err
    assert f"{sig.name} received" in err
    assert "STOPPING=1" in after, after
    store = d / "node.db"
    assert store.exists()
    assert not (d / "node.db-wal").exists(), "store not closed: WAL left behind"
    assert not (d / "node.db-shm").exists()
    _no_lock_left(store)
    outbox = d / "node.db.alerts-outbox"
    assert outbox.exists()
    assert not (d / "node.db.alerts-outbox-journal").exists()
    _no_lock_left(outbox)
    db = sqlite3.connect(outbox)
    attempts = db.execute("SELECT max(attempts) FROM outbox").fetchone()[0]
    db.close()
    assert attempts >= 1  # the queued alerts were being delivered (and stay queued)


# --- unit level ----------------------------------------------------------------------


class FakeDevice:
    def __init__(self, stopper=None):
        self.stopper = stopper
        self.swept: list[str] = []
        self.closed = False

    def sweep(self, plan, cycles=1):
        from quietroom.radio.recorded import streaming_demo_device

        self.swept.append(plan)
        if self.stopper is not None:
            self.stopper.set("SIGTERM")  # the signal lands mid-sweep
        dev = streaming_demo_device(n_clean=0)
        yield next(iter(dev.sweep(plan, cycles=1)))

    def probe(self):
        from quietroom.radio.recorded import streaming_demo_device

        return streaming_demo_device(n_clean=0).probe()

    def close(self):
        self.closed = True


def _two_segment_config():
    return NodeConfig.from_dict(
        {
            "node": {"id": "n", "poll_s": 0.1, "baseline_cycles": 1},
            "segments": [
                {"name": "a", "start_hz": 300e6, "stop_hz": 316e6, "interval_s": 1},
                {"name": "b", "start_hz": 400e6, "stop_hz": 416e6, "interval_s": 1},
            ],
        }
    )


def test_a_signal_mid_sweep_finishes_that_segment_and_abandons_the_rest():
    stopper = Stopper()
    svc = MonitorService(_two_segment_config(), FakeDevice())
    svc.start()
    svc.device = FakeDevice(stopper)
    svc.stop_event = stopper
    r = svc.tick(now=1e9)
    assert r.segments_run == ["a"]
    assert r.abandoned == ["b"]
    assert r.errors == []


class Recorder:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def __getattr__(self, attr):
        def call(*a, **k):
            self.log.append(f"{self.name}.{attr}")
            return 0

        return call


class Channel:
    name = "webhook"
    last_parked: list = []

    def __init__(self, log):
        self.log = log

    def __call__(self, alert):
        pass

    def flush(self, *, budget_s=None, should_stop=None):
        self.log.append("alerts.flush")

    def close(self):
        self.log.append("alerts.close")


def test_run_shuts_down_in_order_when_stopped():
    log: list[str] = []
    stopper = Stopper()
    notifier = SdNotifier(sender=lambda m: log.append(f"notify {m.split('=')[0]}"))
    svc = MonitorService(
        _two_segment_config(),
        FakeDevice(),
        store=Recorder(log, "store"),
        notifiers=[Channel(log)],
        fleet=Recorder(log, "fleet"),
        notifier=notifier,
    )
    stopper.set("SIGTERM")  # already asked to stop before the first tick
    assert svc.run(stop_event=stopper) == 0
    shutdown = log[log.index("notify STOPPING"):]
    assert shutdown[0] == "notify STOPPING"
    assert "alerts.flush" in shutdown and "fleet.flush" in shutdown
    assert shutdown.index("alerts.flush") < shutdown.index("alerts.close")
    assert shutdown.index("fleet.flush") < shutdown.index("fleet.close")
    assert "store.close" in shutdown
    assert svc.device.closed


def test_shutdown_flush_failures_do_not_prevent_closing():
    log: list[str] = []

    class Broken(Channel):
        def flush(self, **kw):
            raise sqlite3.OperationalError("disk I/O error")

    svc = MonitorService(
        _two_segment_config(), FakeDevice(), store=Recorder(log, "store"),
        notifiers=[Broken(log)],
    )
    stopper = Stopper()
    stopper.set("SIGTERM")
    svc.run(stop_event=stopper)
    assert "alerts.close" in log and "store.close" in log


def test_stopper_wakes_a_wait_on_a_real_signal():
    stopper = Stopper()
    stopper.install()
    try:
        t0 = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        assert stopper.wait(5) is True
        assert time.monotonic() - t0 < 1
        assert stopper.signame == "SIGTERM"
    finally:
        stopper.restore()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_stopper_wait_times_out_when_not_signalled():
    stopper = Stopper()
    t0 = time.monotonic()
    assert stopper.wait(0.2) is False
    assert 0.15 < time.monotonic() - t0 < 2
    assert not stopper.is_set()


def test_sd_notifier_stopping():
    sent = []
    SdNotifier(sender=sent.append).stopping()
    assert sent == ["STOPPING=1"]


def test_systemd_unit_gives_the_clean_stop_time_and_uses_sigterm():
    unit = (Path(__file__).parent.parent / "packaging/systemd/quietroom-node.service").read_text()
    lines = [ln.strip() for ln in unit.splitlines()]
    assert "TimeoutStopSec=60" in lines
    assert not any(ln.startswith("KillSignal=") for ln in lines)  # default SIGTERM
    assert "Type=notify" in lines and "NotifyAccess=main" in lines  # STOPPING=1 is accepted


def test_real_daemon_stop_stays_inside_the_shutdown_budget_with_a_slow_sink(short_dir):
    """A relay that is up but slow (0.3 s a request) and 200 queued alerts.

    Unbounded, the final flush would take ~60 s, the whole TimeoutStopSec, and
    systemd would SIGKILL the node mid-send. With ``shutdown_flush_s = 1.5`` the
    stop ends within the budget plus the one request in flight, and the rest
    stays queued on disk.
    """
    import http.server
    import threading

    from quietroom.alerts import Outbox

    hits = []

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            time.sleep(0.3)
            hits.append(time.monotonic())
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = short_dir
    ob = Outbox(d / "node.db.alerts-outbox", channel="webhook")
    for i in range(200):
        ob.add({"type": "finding", "i": i})
    ob.close()
    cfg = d / "node.toml"
    cfg.write_text(
        f"""
[node]
id = "budget-test"
poll_s = 0.5
shutdown_flush_s = 1.5
[store]
path = "{d / 'node.db'}"
[alerts]
webhook_url = "http://127.0.0.1:{srv.server_port}/hook"
min_score = 99
flush_budget_s = 1.0
[[segments]]
name = "covert-uhf"
start_hz = 300_000_000
stop_hz = 316_000_000
interval_s = 1.0
"""
    )
    sock_path = str(d / "notify")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(sock_path)
    proc = subprocess.Popen(
        [*NODE, "--config", str(cfg), "--demo"],
        env={**os.environ, "NOTIFY_SOCKET": sock_path},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        start = _recv_until(sock, "WATCHDOG=1", time.monotonic() + 30)
        assert "WATCHDOG=1" in start, start  # one tick done: its flush was cut at ~1 s
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
        took = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        srv.shutdown()
        sock.close()
    assert proc.returncode == 0, err
    # the in-tick flush may be mid-request when the signal lands, then 1.5 s
    # shutdown budget plus the request in flight
    assert took < 0.3 + 1.5 + 0.3 + 1.5, f"took {took:.2f} s to stop"
    assert hits, "the slow sink was used"
    left = Outbox(d / "node.db.alerts-outbox", channel="webhook")
    try:
        assert 0 < left.pending() < 200  # some delivered, the rest still queued
        assert left.pending() + len(hits) == 200  # nothing lost
    finally:
        left.close()
