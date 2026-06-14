import pytest

from quietroom.radio import hackrf
from quietroom.radio.device import DeviceInfo


def test_build_sweep_cmd_uses_mhz_range_and_bin_hz():
    cmd = hackrf.build_sweep_cmd(100_000_000, 200_000_000, 1_000_000)
    assert "-f" in cmd and "100:200" in cmd
    assert cmd[cmd.index("-w") + 1] == "1000000"
    assert "-l" in cmd and "-g" in cmd
    assert "-a" not in cmd  # amp off by default


def test_build_sweep_cmd_amp_flag():
    cmd = hackrf.build_sweep_cmd(100_000_000, 200_000_000, 1_000_000, amp=True)
    assert cmd[cmd.index("-a") + 1] == "1"


def test_build_iq_cmd_streams_to_stdout():
    cmd = hackrf.build_iq_cmd(101_100_000, 2_000_000, 200_000)
    assert cmd[cmd.index("-r") + 1] == "-"
    assert cmd[cmd.index("-f") + 1] == "101100000"
    assert cmd[cmd.index("-s") + 1] == "2000000"
    assert cmd[cmd.index("-n") + 1] == "200000"


def test_build_sweep_cmd_rejects_out_of_range():
    with pytest.raises(ValueError):
        hackrf.build_sweep_cmd(0, 200_000_000, 1_000_000)  # below 1 MHz
    with pytest.raises(ValueError):
        hackrf.build_sweep_cmd(100_000_000, 7_000_000_000, 1_000_000)  # above 6 GHz


def test_parse_probe_output_present():
    text = (
        "Found HackRF\n"
        "Index: 0\n"
        "Serial number: 0000000000000000457863dc2f4f1d23\n"
        "Board ID Number: 2 (HackRF One)\n"
        "Firmware Version: 2024.02.1 (API:1.08)\n"
    )
    info = hackrf.parse_probe_output(text)
    assert isinstance(info, DeviceInfo)
    assert info.serial.endswith("2f4f1d23")
    assert "HackRF One" in info.board
    assert info.firmware.startswith("2024.02.1")


def test_parse_probe_output_absent():
    assert hackrf.parse_probe_output("No HackRF boards found.") is None


def test_valid_gain_edges_accepted():
    # LNA 0-40 step 8, VGA 0-62 step 2: the extreme valid values must pass.
    hackrf.build_sweep_cmd(100_000_000, 200_000_000, 1_000_000, lna_gain=0, vga_gain=0)
    hackrf.build_sweep_cmd(100_000_000, 200_000_000, 1_000_000, lna_gain=40, vga_gain=62)
    hackrf.build_iq_cmd(101_100_000, 2_000_000, 200_000, lna_gain=8, vga_gain=2)


@pytest.mark.parametrize("lna", [-8, 7, 17, 48])
def test_invalid_lna_gain_rejected(lna):
    with pytest.raises(ValueError):
        hackrf.build_sweep_cmd(100_000_000, 200_000_000, 1_000_000, lna_gain=lna)
    with pytest.raises(ValueError):
        hackrf.build_iq_cmd(101_100_000, 2_000_000, 200_000, lna_gain=lna)


@pytest.mark.parametrize("vga", [-2, 3, 63, 64])
def test_invalid_vga_gain_rejected(vga):
    with pytest.raises(ValueError):
        hackrf.build_sweep_cmd(100_000_000, 200_000_000, 1_000_000, vga_gain=vga)
    with pytest.raises(ValueError):
        hackrf.build_iq_cmd(101_100_000, 2_000_000, 200_000, vga_gain=vga)
