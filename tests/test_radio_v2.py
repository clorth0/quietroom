import pytest
from quietroom.radio.capabilities import check_plan
from quietroom.radio.rtlsdr import RtlSdrDevice, build_rtl_power_cmd, parse_rtl_power_line
from quietroom.radio.device import DeviceError
from quietroom.engine.plan import SweepPlan


def test_capabilities_rtl_vs_hackrf():
    rtl = RtlSdrDevice().capabilities()
    assert rtl.max_hz < 2_000_000_000 and rtl.tx_capable is False
    assert rtl.min_hz >= 400_000


def test_check_plan_rejects_out_of_range():
    rtl = RtlSdrDevice().capabilities()
    with pytest.raises(DeviceError):
        check_plan(rtl, SweepPlan.search(2_400_000_000, 2_410_000_000, 25_000))
    check_plan(rtl, SweepPlan.search(430_000_000, 440_000_000, 25_000))


def test_rtl_power_cmd_and_parse():
    cmd = build_rtl_power_cmd(430_000_000, 440_000_000, 25_000, gain=30)
    assert cmd[0].endswith("rtl_power") and "430000000:440000000:25000" in cmd
    freqs, power = parse_rtl_power_line(
        "2024-01-01, 00:00:00, 430000000, 440000000, 25000.0, 10, -90.0, -80.0"
    )
    assert freqs[0] == 430_012_500 and power[1] == -80.0


class FakeRun:
    pass


def test_rtlsdr_sweep_via_injected_runner():
    lines = [
        "d, t, 430000000, 435000000, 25000.0, 10, " + " ,".join(["-90.0"] * 200),
        "d, t, 435000000, 440000000, 25000.0, 10, " + " ,".join(["-90.0"] * 200),
    ]
    dev = RtlSdrDevice(runner=lambda cmd, cycles: iter(lines))
    (ps,) = list(dev.sweep(SweepPlan.search(430_000_000, 440_000_000, 25_000), cycles=1))
    assert len(ps.power_dbm) == 400
