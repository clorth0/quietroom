# Quietroom

[![CI](https://github.com/clorth0/quietroom/actions/workflows/ci.yml/badge.svg)](https://github.com/clorth0/quietroom/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/clorth0/quietroom)](LICENSE)

Receive-only RF bug-sweep tool. Quietroom drives a HackRF One to hunt for hidden
transmitters in a space by finding emitters that do not belong, then ranks them
by how suspicious they are and tells you why in plain language.

It is a technical surveillance countermeasures (TSCM) aid for sweeping spaces you
own or are authorized to sweep. See [SECURITY.md](SECURITY.md) for scope and
intended use.

## Status

Alpha (v0.2.x). The detection engine, HackRF radio layer, audio-correlation
test, terminal CLI, and localhost web UI are all implemented and covered by a
hardware-free test suite. Detection thresholds and scoring weights are still
being tuned against real sweeps; treat scores as triage, not proof.

## Install

Requirements:

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- HackRF host tools on your `PATH` (`hackrf_info`, `hackrf_sweep`,
  `hackrf_transfer`): `brew install hackrf` (macOS) or
  `sudo apt install hackrf` (Debian/Ubuntu)
- For the audio-correlation test only: PortAudio (`brew install portaudio` /
  `sudo apt install libportaudio2`) plus a speaker and microphone in the room

```
git clone https://github.com/clorth0/quietroom.git
cd quietroom
uv sync --extra dev        # runtime deps + pytest
uv run pytest              # hardware-free test suite
uv run quietroom --demo    # try it with no HackRF attached
```

## Usage

Frequencies accept `k`/`M`/`G` suffixes (`300M`, `433.92MHz`, `2.4G`, `25k`);
bare numbers are Hz.

```
# Sweep 300-470 MHz at 25 kHz resolution: capture a baseline, then look for
# new emitters. Keep the room quiet and suspected bugs "off" (or out of the
# room) during the baseline if you can.
uv run quietroom --start 300M --stop 470M --bin 25k

# Show why each suspect scored what it did
uv run quietroom --start 300M --stop 470M --bin 25k -v

# Tune sensitivity: lower -k flags weaker changes; gains are HackRF LNA/VGA
uv run quietroom --start 2.4G --stop 2.5G --bin 100k -k 3 --lna 24 --vga 30

# Audio-correlation test on a suspect: plays a chirp in the room and checks
# whether the emitter's envelope follows it
uv run quietroom --investigate 308M
```

Example (demo) output:

```
SCORE    FREQ (MHz)  BAND                      REASONS
   85       308.000  unknown                   not in any known band, near-field strong signal, in common analog bug band
                                               score: snr=+20 catalog=+30 signatures=+35
```

Run `uv run quietroom --help` for every option.

### Notes

- `hackrf_sweep` works in whole MHz, so sweep edges are rounded outward to the
  nearest MHz.
- A suspect that occupies a single coarse bin cannot be proven narrowband;
  rescan with a finer `--bin` around it.
- Scores are a 0-100 sum of: excess over baseline (capped at 20), the
  known-band catalog (+30 unknown / negative in known bands), bug-signature
  heuristics, and, after `--investigate`, the audio test (+50).

## How it works

Quietroom learns a clean RF baseline of an area, then sweeps it live and flags
anything new or unexplained. Each candidate signal is scored by four detectors:

- **Baseline differencing** — what changed versus the clean profile.
- **Known-emitter subtraction** — is it in a known broadcast / WiFi / cellular /
  ISM band, or unexplained?
- **Bug-signature heuristics** — narrowband persistent carrier, near-field
  strength, common covert-transmitter bands.
- **Audio-correlation test** — play a test tone in the room and check whether a
  candidate's signal envelope moves with the sound. A strong tell for a live
  audio bug.

Findings are presented live in a browser (waterfall plus a ranked suspect list),
with a physical hot/cold locator planned for a later phase.

## Roadmap

- **Phase 1 (v1):** sweep-now room scan with the four-detector scoring engine.
- **Phase 2:** leave-and-monitor mode with new-signal alerting.
- **Phase 3:** physical locator (live RSSI meter, hot/cold homing).

## Hardware

HackRF One (1 MHz to 6 GHz). A wideband antenna and, optionally, a bias-tee LNA
improve sensitivity. The device layer is pluggable so other SDRs can be added
later.

## Web UI

A localhost browser interface (live waterfall, ranked suspect table, on-demand
audio investigate):

    uv run quietroom-web            # live HackRF
    uv run quietroom-web --demo     # no hardware

Then open http://127.0.0.1:8770. It binds localhost only; reach it remotely over
an SSH tunnel or Tailscale, not a public interface.

## License

MIT. See [LICENSE](LICENSE).
