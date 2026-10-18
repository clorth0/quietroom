"""RTL-SDR tier: cheap receive-only nodes for sub-GHz/low-GHz segments.

Uses rtl_power for sweeps and rtl_sdr for IQ (librtlsdr tools), mirroring the
HackRF subprocess approach. Coverage is roughly 500 kHz–1.75 GHz (tuner
dependent): ideal for 433/868/915 MHz and analog-bug bands, useless for
2.4/5 GHz — the scheduler must check capabilities() before assigning plans.
"""

from __future__ import annotations
import shutil
import subprocess
import time
import numpy as np
from quietroom.engine.spectrum import PowerSpectrum
from quietroom.radio.capabilities import RadioCapabilities, check_plan
from quietroom.radio.device import Device, DeviceError, DeviceInfo
from quietroom.radio.limits import RTL_MAX_HZ as MAX_HZ
from quietroom.radio.limits import RTL_MIN_HZ as MIN_HZ

RTL_POWER = shutil.which("rtl_power") or "rtl_power"
RTL_SDR = shutil.which("rtl_sdr") or "rtl_sdr"


def build_rtl_power_cmd(f_start_hz, f_stop_hz, bin_hz, gain=40):
    if not (0 <= gain <= 50):
        raise ValueError("RTL-SDR gain must be 0-50 dB")
    return [RTL_POWER, "-f", f"{f_start_hz}:{f_stop_hz}:{bin_hz}", "-g", str(gain), "-"]


def parse_rtl_power_line(line):
    parts = [p.strip() for p in line.split(",")]
    hz_low, hz_high, step = int(parts[2]), int(parts[3]), float(parts[4])
    powers = [float(p) for p in parts[6:]]
    freqs = np.arange(hz_low + step / 2, hz_high, step)[: len(powers)]
    return freqs.astype(float), np.array(powers, dtype=float)


class RtlSdrDevice(Device):
    def __init__(self, gain=40, runner=None):
        self.gain = gain
        self._runner = runner

    def capabilities(self):
        return RadioCapabilities(
            "RTL-SDR",
            MIN_HZ,
            MAX_HZ,
            2_400_000,
            tx_capable=False,
            calibrated=False,
            notes="receive-only, ~500 kHz-1.75 GHz (tuner dependent)",
        )

    def probe(self):
        try:
            r = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True, timeout=3)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return (
            DeviceInfo("rtlsdr", "RTL-SDR", "")
            if r.returncode == 0 or "Found" in (r.stdout + r.stderr)
            else None
        )

    def _lines(self, cmd, cycles):
        if self._runner is not None:
            yield from self._runner(cmd, cycles)
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            yield from proc.stdout
        finally:
            proc.terminate()

    def sweep(self, plan, cycles=1):
        check_plan(self.capabilities(), plan)
        cmd = build_rtl_power_cmd(plan.start_hz, plan.stop_hz, plan.bin_hz, self.gain)
        bins = {}
        emitted = 0
        for line in self._lines(cmd, cycles):
            if not line.strip():
                continue
            try:
                freqs, powers = parse_rtl_power_line(line)
            except (IndexError, ValueError):
                continue
            if len(freqs) and float(freqs[0]) in bins and emitted < cycles:
                yield self._flush(bins)
                emitted += 1
                bins = {}
                if emitted >= cycles:
                    return
            for f, p in zip(freqs, powers, strict=False):
                bins[float(f)] = float(p)
        if bins and emitted < cycles:
            yield self._flush(bins)

    @staticmethod
    def _flush(bins):
        f = np.array(sorted(bins))
        return PowerSpectrum(f, np.array([bins[x] for x in f]), time.time())

    def capture_iq(self, center_hz, sample_rate, n_samples):
        if not (MIN_HZ <= center_hz <= MAX_HZ):
            raise DeviceError("center outside RTL-SDR range")
        cmd = [RTL_SDR, "-f", str(center_hz), "-s", str(sample_rate), "-n", str(n_samples), "-"]
        try:
            raw = subprocess.run(cmd, capture_output=True, timeout=30).stdout
        except FileNotFoundError as e:
            raise DeviceError("rtl_sdr not found; install rtl-sdr tools") from e
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        n = (len(a) // 2) * 2
        a = (a[:n] - 127.5) / 127.5
        return (a[0::2] + 1j * a[1::2]).astype(np.complex64)
