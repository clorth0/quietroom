import numpy as np
import pytest
from quietroom.config import NodeConfig, load_config
from quietroom.node.scheduler import BandScheduler
from quietroom.node.service import MonitorService
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.radio.device import Device, DeviceInfo

TOML = """
[node]
id="node-01"
site="hq"
floor="3"
room="lab"
region="us"
threshold_k=4.0
baseline_cycles=3
[[segments]]
name="covert-uhf"
start_hz=300_000_000
stop_hz=320_000_000
bin_hz=25_000
interval_s=60
priority=10
[[segments]]
name="wifi24"
start="2.4G"
stop="2.5G"
bin="100k"
mode="overview"
interval_s=300
priority=5
"""


def test_config_loads_segments_and_freq_strings(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(TOML)
    c = load_config(p)
    assert c.node_id == "node-01" and c.floor == "3"
    assert [s.name for s in c.segments] == ["covert-uhf", "wifi24"]
    assert c.segments[1].plan.start_hz == 2_400_000_000


def test_config_rejects_bad_segment(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(TOML.replace("interval_s=60", "interval_s=0", 1))
    with pytest.raises(ValueError):
        load_config(p)


def test_scheduler_priority_and_interval():
    c = NodeConfig.from_dict(
        {
            "node": {"id": "n"},
            "segments": [
                {
                    "name": "a",
                    "start_hz": 300_000_000,
                    "stop_hz": 320_000_000,
                    "bin_hz": 25000,
                    "interval_s": 60,
                    "priority": 1,
                },
                {
                    "name": "b",
                    "start_hz": 400_000_000,
                    "stop_hz": 420_000_000,
                    "bin_hz": 25000,
                    "interval_s": 60,
                    "priority": 9,
                },
            ],
        }
    )
    s = BandScheduler(c.segments)
    assert [x.name for x in s.due(1000.0, {})] == ["b", "a"]
    assert [x.name for x in s.due(1030.0, {"a": 1000.0, "b": 1000.0})] == []
    assert [x.name for x in s.due(1061.0, {"a": 1000.0, "b": 1000.0})] == ["b", "a"]


class Fake(Device):
    def probe(self):
        return DeviceInfo("s", "Fake", "1")

    def sweep(self, plan, cycles=1):
        f = np.arange(plan.start_hz, plan.stop_hz, plan.bin_hz, dtype=float)
        base = np.full(len(f), -95.0)
        for cyc in range(cycles):
            p = base.copy()
            if cyc >= 2 and len(p) > 10:
                p[8] = -30.0
            yield PowerSpectrum(f, p, float(cyc))

    def capture_iq(self, center_hz, sample_rate, n_samples):
        return np.zeros(n_samples, dtype=np.complex64)


def test_service_baselines_ticks_and_emits():
    c = NodeConfig.from_dict(
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
    events = []
    svc = MonitorService(c, Fake(), sink=events.append)
    svc.start()
    r = svc.tick(100.0)
    assert r.segments_run == ["uhf"]
    assert events and events[0]["node_id"] == "n1"
    assert svc.health()["baselines"] == ["uhf"]
    assert svc.tick(105.0).segments_run == []


def test_node_cli_check_config(tmp_path, capsys):
    from quietroom.node.cli import main

    p = tmp_path / "c.toml"
    p.write_text(TOML)
    assert main(["--config", str(p), "--check-config"]) == 0
    assert "node-01" in capsys.readouterr().out
