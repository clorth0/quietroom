import numpy as np
from quietroom.config import NodeConfig
from quietroom.node.service import MonitorService
from quietroom.node.watchdog import SdNotifier
from quietroom.store import Store
from quietroom.alerts import AlertEngine, AlertRule
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.radio.device import Device, DeviceInfo


class Fake(Device):
    def probe(self):
        return DeviceInfo("s", "Fake", "1")

    def sweep(self, plan, cycles=1):
        f = np.arange(plan.start_hz, plan.stop_hz, plan.bin_hz, dtype=float)
        base = np.full(len(f), -95.0)
        for c in range(cycles):
            p = base.copy()
            if cycles == 1 and len(p) > 10:
                p[8] = -30.0
            yield PowerSpectrum(f, p, float(c))

    def capture_iq(self, c, s, n):
        return np.zeros(n, dtype=np.complex64)


def cfg():
    return NodeConfig.from_dict(
        {
            "node": {"id": "n1"},
            "segments": [
                {
                    "name": "uhf",
                    "start_hz": 300_000_000,
                    "stop_hz": 301_000_000,
                    "bin_hz": 25000,
                    "interval_s": 10,
                    "priority": 1,
                }
            ],
        }
    )


def test_service_writes_store_alerts_and_fleet(tmp_path):
    store = Store(tmp_path / "q.db")
    alerts = []
    fleet_events = []
    heartbeats = []

    class Fleet:
        def send_events(self, node_id, events):
            fleet_events.extend(events)
            return True

        def send_heartbeat(self, p):
            heartbeats.append(p)
            return True

    svc = MonitorService(
        cfg(),
        Fake(),
        store=store,
        alert_engine=AlertEngine([AlertRule(min_score=50, cooldown_s=60)]),
        notifiers=[alerts.append],
        fleet=Fleet(),
        fleet_interval_s=0,
    )
    svc.start()
    svc.tick(100.0)
    assert store.sweep_count("n1") == 1 and store.findings("n1")
    assert alerts and alerts[0]["node_id"] == "n1"
    assert fleet_events and heartbeats


def test_service_reuses_stored_baseline(tmp_path):
    store = Store(tmp_path / "q.db")
    svc = MonitorService(cfg(), Fake(), store=store)
    svc.start()
    assert store.baseline_versions("n1", "uhf") == [1]
    svc2 = MonitorService(cfg(), Fake(), store=store)
    svc2.start()
    assert store.baseline_versions("n1", "uhf") == [1]  # loaded, not recaptured


def test_sd_notify_messages():
    sent = []
    n = SdNotifier(socket_path=None, sender=sent.append)
    n.ready()
    n.watchdog()
    n.status("ok")
    assert sent == ["READY=1", "WATCHDOG=1", "STATUS=ok"]


def test_config_sections_parse(tmp_path):
    from quietroom.config import load_config

    p = tmp_path / "c.toml"
    p.write_text("""
[node]
id="n1"
[store]
path="/var/lib/quietroom/q.db"
[alerts]
min_score=70
cooldown_s=120
webhook_url="https://example.test/hook"
[fleet]
base_url="https://central.test"
heartbeat_s=60
[[segments]]
name="uhf"
start_hz=300000000
stop_hz=301000000
bin_hz=25000
interval_s=10
""")
    c = load_config(p)
    assert c.store_path == "/var/lib/quietroom/q.db" and c.alert_min_score == 70
    assert c.fleet_base_url == "https://central.test" and c.fleet_heartbeat_s == 60
