from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def _unit(): return (ROOT/"packaging/systemd/quietroom-node.service").read_text()

def test_systemd_unit_is_hardened_and_watchdogged():
    u=_unit()
    needles = [
        "ExecStart=/usr/local/bin/quietroom-node --config /etc/quietroom/config.toml",
        "User=quietroom", "Restart=on-failure", "WatchdogSec=120", "NotifyAccess=main",
        "NoNewPrivileges=true", "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/quietroom", "WantedBy=multi-user.target",
    ]
    for needle in needles:
        assert needle in u, needle

def test_udev_and_install_script():
    assert "hackrf" in (ROOT/"packaging/udev/99-quietroom-sdr.rules").read_text().lower()
    s=(ROOT/"packaging/install.sh").read_text()
    assert "systemctl enable" in s and "/etc/quietroom" in s and "set -euo pipefail" in s

def test_bench_reports_bins_and_ms():
    from tools.bench_sweep import bench
    r=bench(300_000_000,320_000_000,25_000,reps=1)
    assert r["bins"]==800 and r["ms_per_sweep"]>=0
