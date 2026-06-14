"""Terminal entry point: run a sweep-now scan and print ranked suspects."""
from __future__ import annotations

import argparse
from collections.abc import Sequence

from quietroom.audio.capture import audio_correlation_test
from quietroom.engine.detectors.audio_corr import AUDIO_CORR_THRESHOLD
from quietroom.engine.spectrum import Finding
from quietroom.radio.device import Device
from quietroom.radio.hackrf import HackRFDevice
from quietroom.radio.recorded import demo_device
from quietroom.scan import capture_baseline, live_findings


def format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "No suspicious emitters found. Room looks clean."
    lines = [f"{'SCORE':>5}  {'FREQ (MHz)':>12}  {'BAND':<24}  REASONS"]
    for f in findings:
        mhz = f.candidate.center_freq_hz / 1e6
        lines.append(
            f"{f.score:5.0f}  {mhz:12.3f}  {f.band_label:<24}  "
            f"{', '.join(f.reasons)}"
        )
    return "\n".join(lines)


def format_verdict(freq_hz: float, correlation: float) -> str:
    mhz = freq_hz / 1e6
    if correlation >= AUDIO_CORR_THRESHOLD:
        return (f"{mhz:.3f} MHz: envelope correlates with room audio at "
                f"{correlation:.2f} -- LIKELY LIVE BUG")
    return (f"{mhz:.3f} MHz: no audio correlation ({correlation:.2f}); "
            f"not a detectable live audio bug")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quietroom", description="Receive-only HackRF bug sweep."
    )
    parser.add_argument("--demo", action="store_true",
                        help="run with no hardware using a synthetic planted bug")
    parser.add_argument("--start", type=int, default=300_000_000,
                        help="sweep start frequency in Hz")
    parser.add_argument("--stop", type=int, default=320_000_000,
                        help="sweep stop frequency in Hz")
    parser.add_argument("--bin", type=int, default=1_000_000,
                        help="sweep bin width in Hz")
    parser.add_argument("--baseline-cycles", type=int, default=5,
                        help="number of sweeps to average into the baseline")
    parser.add_argument("--investigate", type=int, default=None,
                        metavar="FREQ_HZ",
                        help="run the audio-correlation test at this frequency (Hz)")
    args = parser.parse_args(argv)

    if args.investigate is not None:
        if args.demo:
            # Synthetic: demonstrate a positive verdict with no hardware.
            correlation = 0.85
        else:
            dev = HackRFDevice()
            if dev.probe() is None:
                print("HackRF not detected. Connect it, or use --demo.")
                return 1
            print("Playing test tone and listening on the suspect frequency...")
            correlation = audio_correlation_test(dev, args.investigate)
        print(format_verdict(float(args.investigate), correlation))
        return 0

    if args.demo:
        dev = demo_device()
        baseline = capture_baseline(dev, args.start, args.stop, args.bin,
                                    cycles=3)
        findings = live_findings(dev, baseline, live=dev.live_sweep())
        print(format_findings(findings))
        return 0

    dev: Device = HackRFDevice()
    if dev.probe() is None:
        print("HackRF not detected. Connect it, or use --demo.")
        return 1
    print("Capturing baseline; keep the area quiet...")
    baseline = capture_baseline(dev, args.start, args.stop, args.bin,
                                cycles=args.baseline_cycles)
    print("Sweeping for suspicious emitters...")
    findings = live_findings(dev, baseline,
                             f_start_hz=args.start, f_stop_hz=args.stop,
                             bin_hz=args.bin)
    print(format_findings(findings))
    return 0
