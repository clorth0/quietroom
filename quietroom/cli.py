"""Terminal entry point: run a sweep-now scan and print ranked suspects."""
from __future__ import annotations

import argparse
import re
from collections.abc import Sequence

from quietroom.bands import REGIONS, bands_for
from quietroom.audio.capture import (
    CaptureFailed,
    LiveBug,
    NoAudioMatch,
    audio_correlation_test,
)
from quietroom.engine.plan import SweepPlan
from quietroom.engine.spectrum import Finding
from quietroom.radio.device import Device
from quietroom.radio.hackrf import (
    DEFAULT_LNA,
    DEFAULT_VGA,
    HackRFDevice,
    _check_gain,
)
from quietroom.radio.recorded import demo_device
from quietroom.scan import capture_baseline, confirmed_findings


_FREQ_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:hz)?\s*$", re.IGNORECASE)
_SCALE = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}


def parse_freq(text: str) -> int:
    """Parse '308M', '433.92MHz', '2.4G', '25k' or bare Hz into integer Hz."""
    m = _FREQ_RE.match(text or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid frequency {text!r}; use e.g. 308M, 433.92MHz, 2.4G, 25k or Hz"
        )
    return int(round(float(m.group(1)) * _SCALE[m.group(2).lower()]))


def format_breakdown(f: Finding) -> str:
    parts = " ".join(f"{k}={v:+.0f}" for k, v in f.breakdown.items())
    parts += f" (raw {f.raw_score:.0f})"
    return parts


def format_findings(findings: list[Finding], verbose: bool = False) -> str:
    if not findings:
        return "No suspicious emitters found. Room looks clean."
    lines = [f"{'SCORE':>5}  {'FREQ (MHz)':>12}  {'BAND':<24}  REASONS"]
    for f in findings:
        mhz = f.candidate.center_freq_hz / 1e6
        lines.append(
            f"{f.score:5.0f}  {mhz:12.3f}  {f.band_label:<24}  "
            f"{', '.join(f.reasons)}"
        )
        if verbose:
            lines.append(f"{'':5}  {'':12}  {'':24}  score: {format_breakdown(f)}")
    return "\n".join(lines)


def format_verdict(
    freq_hz: float, verdict: LiveBug | NoAudioMatch | CaptureFailed
) -> str:
    mhz = freq_hz / 1e6
    if isinstance(verdict, LiveBug):
        return (f"{mhz:.3f} MHz: envelope correlates with room audio at "
                f"{verdict.correlation:.2f} -- LIKELY LIVE BUG")
    if isinstance(verdict, NoAudioMatch):
        return (f"{mhz:.3f} MHz: no audio correlation ({verdict.correlation:.2f}); "
                f"not a detectable live audio bug")
    if isinstance(verdict, CaptureFailed):
        return f"capture failed: {verdict.reason}"
    raise TypeError("format_verdict requires LiveBug, NoAudioMatch, or CaptureFailed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quietroom",
        description="Receive-only HackRF bug sweep. Frequencies accept k/M/G "
                    "suffixes (e.g. 300M, 433.92MHz, 2.4G); bare numbers are Hz.",
    )
    parser.add_argument("--demo", action="store_true",
                        help="run with no hardware using a synthetic planted bug")
    default_plan = SweepPlan.search()
    parser.add_argument("--start", type=parse_freq, default=default_plan.start_hz,
                        help="sweep start frequency (default 300M)")
    parser.add_argument("--stop", type=parse_freq, default=default_plan.stop_hz,
                        help="sweep stop frequency (default 320M)")
    parser.add_argument("--bin", type=parse_freq, default=default_plan.bin_hz,
                        help="sweep bin width (default 25k; 2445 Hz-5M). Use a "
                             "coarser bin (e.g. 1M) with --overview for multi-GHz spans")
    parser.add_argument("--overview", action="store_true",
                        help="allow a bin wider than the narrowband threshold")
    parser.add_argument("--baseline-cycles", type=int, default=5,
                        help="number of sweeps to average into the baseline")
    parser.add_argument("--investigate", type=parse_freq, default=None,
                        metavar="FREQ",
                        help="run the audio-correlation test at this frequency")
    parser.add_argument("-k", "--threshold", dest="k", type=float, default=4.0,
                        help="flag bins this many std-devs above baseline (default 4)")
    parser.add_argument("--lna", type=int, default=DEFAULT_LNA,
                        help=f"HackRF LNA/IF gain dB, 0-40 step 8 (default {DEFAULT_LNA})")
    parser.add_argument("--vga", type=int, default=DEFAULT_VGA,
                        help=f"HackRF VGA/baseband gain dB, 0-62 step 2 (default {DEFAULT_VGA})")
    parser.add_argument("--amp", action="store_true",
                        help="enable the HackRF RF front-end amp (+~11 dB)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show each finding's score breakdown")
    parser.add_argument("--region", choices=sorted(REGIONS), default="us",
                        help="band catalog used to explain known emitters")
    parser.add_argument("--live-sweeps", type=int, default=3,
                        help="live sweeps to take after the baseline")
    parser.add_argument("--min-hits", type=int, default=2,
                        help="report only emitters seen in at least this many live sweeps")
    args = parser.parse_args(argv)
    if args.live_sweeps < 1 or not (1 <= args.min_hits <= args.live_sweeps):
        parser.error("need --live-sweeps >= 1 and 1 <= --min-hits <= --live-sweeps")
    bands = bands_for(args.region)
    try:
        _check_gain(args.lna, args.vga)
    except ValueError as e:
        parser.error(str(e))
    if args.k <= 0:
        parser.error("-k must be positive")
    try:
        plan = (SweepPlan.overview if args.overview else SweepPlan.search)(
            args.start, args.stop, args.bin,
        )
    except ValueError as e:
        parser.error(str(e))

    if args.investigate is not None:
        if args.demo:
            # Run the real DSP on a synthetic room: the demo device emits an AM
            # bug carrying the test chirp, and the "mic" is a loopback.
            verdict = audio_correlation_test(
                demo_device(), args.investigate,
                play_record=lambda sig, fs: sig.astype(float),
            )
        else:
            radio = HackRFDevice(args.lna, args.vga, args.amp)
            if radio.probe() is None:
                print("HackRF not detected. Connect it, or use --demo.")
                return 1
            print("Playing test tone and listening on the suspect frequency...")
            verdict = audio_correlation_test(radio, args.investigate)
        print(format_verdict(float(args.investigate), verdict))
        return 1 if isinstance(verdict, CaptureFailed) else 0

    if args.demo:
        demo = demo_device()
        baseline = capture_baseline(demo, plan, cycles=3)
        findings = confirmed_findings(
            demo, baseline, args.live_sweeps, args.min_hits, plan,
            live=demo.live_sweep(plan),
            bands=bands, k=args.k,
        )
        print(format_findings(findings, verbose=args.verbose))
        return 0

    dev: Device = HackRFDevice(args.lna, args.vga, args.amp)
    if dev.probe() is None:
        print("HackRF not detected. Connect it, or use --demo.")
        return 1
    print("Capturing baseline; keep the area quiet...")
    baseline = capture_baseline(dev, plan, cycles=args.baseline_cycles)
    print("Sweeping for suspicious emitters...")
    findings = confirmed_findings(
        dev, baseline, args.live_sweeps, args.min_hits, plan,
        bands=bands, k=args.k,
    )
    print(format_findings(findings, verbose=args.verbose))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
