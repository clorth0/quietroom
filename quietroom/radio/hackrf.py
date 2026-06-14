"""HackRF One driver: command construction, probing, and subprocess capture."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Iterator

import numpy as np

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.radio.device import Device, DeviceError, DeviceInfo
from quietroom.radio.parse import (
    SweepAccumulator,
    iq_cs8_to_complex,
    parse_sweep_line,
)

HACKRF_SWEEP = shutil.which("hackrf_sweep") or "/opt/homebrew/bin/hackrf_sweep"
HACKRF_TRANSFER = shutil.which("hackrf_transfer") or "/opt/homebrew/bin/hackrf_transfer"
HACKRF_INFO = shutil.which("hackrf_info") or "/opt/homebrew/bin/hackrf_info"

MIN_HZ = 1_000_000
MAX_HZ = 6_000_000_000
DEFAULT_LNA = 16
DEFAULT_VGA = 20
IQ_BASEBAND_HZ = 1_750_000
# HackRF gain ranges: LNA (IF) 0-40 dB in 8 dB steps, VGA (baseband) 0-62 dB in 2 dB steps.
LNA_MAX_DB = 40
LNA_STEP_DB = 8
VGA_MAX_DB = 62
VGA_STEP_DB = 2


def _check_range(f_start_hz: int, f_stop_hz: int) -> None:
    if not (MIN_HZ <= f_start_hz < f_stop_hz <= MAX_HZ):
        raise ValueError(
            f"frequency range {f_start_hz}-{f_stop_hz} Hz outside "
            f"{MIN_HZ}-{MAX_HZ} Hz"
        )


def _check_gain(lna_gain: int, vga_gain: int) -> None:
    if not (0 <= lna_gain <= LNA_MAX_DB and lna_gain % LNA_STEP_DB == 0):
        raise ValueError(
            f"LNA gain {lna_gain} must be 0-{LNA_MAX_DB} dB in steps of {LNA_STEP_DB}"
        )
    if not (0 <= vga_gain <= VGA_MAX_DB and vga_gain % VGA_STEP_DB == 0):
        raise ValueError(
            f"VGA gain {vga_gain} must be 0-{VGA_MAX_DB} dB in steps of {VGA_STEP_DB}"
        )


def build_sweep_cmd(
    f_start_hz: int,
    f_stop_hz: int,
    bin_hz: int,
    lna_gain: int = DEFAULT_LNA,
    vga_gain: int = DEFAULT_VGA,
    amp: bool = False,
) -> list[str]:
    _check_range(f_start_hz, f_stop_hz)
    _check_gain(lna_gain, vga_gain)
    cmd = [
        HACKRF_SWEEP,
        "-f", f"{f_start_hz // 1_000_000}:{f_stop_hz // 1_000_000}",
        "-w", str(bin_hz),
        "-l", str(lna_gain),
        "-g", str(vga_gain),
    ]
    if amp:
        cmd += ["-a", "1"]
    return cmd


def build_iq_cmd(
    center_hz: int,
    sample_rate: int,
    n_samples: int,
    lna_gain: int = DEFAULT_LNA,
    vga_gain: int = DEFAULT_VGA,
    amp: bool = False,
) -> list[str]:
    if not (MIN_HZ <= center_hz <= MAX_HZ):
        raise ValueError(f"center {center_hz} Hz outside {MIN_HZ}-{MAX_HZ} Hz")
    _check_gain(lna_gain, vga_gain)
    cmd = [
        HACKRF_TRANSFER,
        "-r", "-",
        "-f", str(center_hz),
        "-s", str(sample_rate),
        "-b", str(IQ_BASEBAND_HZ),
        "-n", str(n_samples),
        "-l", str(lna_gain),
        "-g", str(vga_gain),
    ]
    if amp:
        cmd += ["-a", "1"]
    return cmd


def parse_probe_output(text: str) -> DeviceInfo | None:
    if "No HackRF boards found" in text or "Found HackRF" not in text:
        return None
    serial = re.search(r"Serial number:\s*(\S+)", text)
    board = re.search(r"Board ID Number:\s*(.+)", text)
    firmware = re.search(r"Firmware Version:\s*(.+)", text)
    return DeviceInfo(
        serial=serial.group(1) if serial else "",
        board=board.group(1).strip() if board else "",
        firmware=firmware.group(1).strip() if firmware else "",
    )


def probe_hackrf(timeout: float = 2.0) -> DeviceInfo | None:
    try:
        result = subprocess.run(
            [HACKRF_INFO], capture_output=True, text=True, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return parse_probe_output(result.stdout)


class HackRFDevice(Device):
    def __init__(
        self,
        lna_gain: int = DEFAULT_LNA,
        vga_gain: int = DEFAULT_VGA,
        amp: bool = False,
    ) -> None:
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp = amp

    def probe(self) -> DeviceInfo | None:
        return probe_hackrf()

    def sweep(
        self, f_start_hz: int, f_stop_hz: int, bin_hz: int, cycles: int = 1
    ) -> Iterator[PowerSpectrum]:
        cmd = build_sweep_cmd(
            f_start_hz, f_stop_hz, bin_hz, self.lna_gain, self.vga_gain, self.amp
        )
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
        )
        if proc.stdout is None:  # pragma: no cover - defensive
            raise DeviceError("hackrf_sweep produced no stdout")
        acc = SweepAccumulator()
        emitted = 0
        try:
            for line in proc.stdout:
                if not line.strip():
                    continue
                hz_low, bin_width, powers = parse_sweep_line(line)
                ps = acc.add(hz_low, bin_width, powers, timestamp=time.time())
                if ps is not None:
                    yield ps
                    emitted += 1
                    if emitted >= cycles:
                        return
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    def capture_iq(
        self, center_hz: int, sample_rate: int, n_samples: int
    ) -> np.ndarray:
        cmd = build_iq_cmd(
            center_hz, sample_rate, n_samples, self.lna_gain, self.vga_gain, self.amp
        )
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        if proc.stdout is None:  # pragma: no cover - defensive
            raise DeviceError("hackrf_transfer produced no stdout")
        want_bytes = n_samples * 2
        try:
            raw = proc.stdout.read(want_bytes)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        return iq_cs8_to_complex(raw)
