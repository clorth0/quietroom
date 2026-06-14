# Quietroom Design

Date: 2026-06-14
Status: Approved (brainstorming complete, pre-implementation)

## 1. Summary

Quietroom is a receive-only RF sweep tool that hunts for hidden transmitters
(bugs) in a space by finding emitters that do not belong. It drives a HackRF One,
learns a clean RF baseline of an area, sweeps the area live, and produces a
ranked list of suspicious signals with plain-language reasons.

It is a technical surveillance countermeasures (TSCM) aid for spaces the operator
owns or is authorized to sweep. It does not transmit, jam, or intercept
communications content.

## 2. Goals and non-goals

### Goals
- Find emitters present now that were not in a trusted baseline.
- Rank candidates by a transparent 0 to 100 suspicion score with human-readable
  reasons, not a black box.
- Be usable in the field from a browser on a laptop or phone.
- Keep the detection logic hardware-independent and unit-testable against
  recorded and synthetic captures.

### Non-goals (v1)
- Non-linear junction detection (finds powered-off bugs; needs special hardware).
- Powerline / carrier-current bug detection.
- True modulation classification or ML-based signal identification.
- Multi-radio fusion or SDRplay RSPdx support.
- Infrared / optical surveillance detection.
- Decoding or recording the content of any communication.

## 3. Primary use modes (phased)

1. **Sweep-now room scan (v1 core).** Walk into a room, run a full-band sweep,
   get a ranked list of suspicious emitters and a report.
2. **Leave-and-monitor (phase 2).** Run continuously over hours or days, alert on
   any new signal versus the learned baseline, track persistence of intermittent
   or burst emitters.
3. **Physical locator (phase 3).** Park on a suspect frequency and present a live
   signal-strength meter plus a rising geiger-counter tone to physically home in
   on the transmitter.

Each phase reuses the prior phase's machinery: the baseline and diff engine from
phase 1 powers phase 2; the tuned-IQ RSSI path used in phase 1's audio test
powers phase 3's locator.

## 4. Architecture

The codebase separates a pure detection engine from the hardware and transport
layers. The engine never imports Flask and never talks to a radio; it takes
spectrum and IQ data in and returns ranked findings out. This is what makes the
detection logic, the valuable and hardest-to-verify part, testable without a
live bug in the room.

```
quietroom/
  engine/            # pure Python. no Flask, no hardware.
    spectrum.py        # data types: PowerSpectrum, Baseline, Candidate, Finding
    baseline.py        # build / store / compare RF baselines (per-bin mean + std)
    score.py           # fuse detector outputs into 0-100 suspicion score + reasons
    detectors/
      diff.py            # baseline differencing (the spine)
      catalog.py         # known-emitter band subtraction
      signatures.py      # bug-signature heuristics
      audio_corr.py      # audio-correlation test
  radio/             # hardware layer (pluggable)
    device.py          # abstract Device interface: sweep() + capture_iq()
    hackrf.py          # HackRF sweep path + tuned-IQ path
  audio/
    capture.py         # microphone capture + test-chirp emission
  bands.py           # frequency -> band-label table (FM, WiFi, cellular, ISM...)
  web/               # thin transport: Flask + Socket.IO
    app.py
    static/, templates/  # waterfall, suspect list, locator meter
  store/             # SigMF recordings + saved baselines (gitignored)
  tests/             # engine tested against recorded + synthetic fixtures
```

### Two RX paths
- **Sweep path.** HackRF wideband sweep across 1 MHz to 6 GHz (or a chosen
  sub-range), producing power versus frequency. Drives baseline building and
  candidate discovery.
- **Tuned-IQ path.** Park on one candidate frequency and capture IQ. Drives the
  audio-correlation test, signature analysis, and the phase-3 locator meter.

## 5. Data types (engine/spectrum.py)

- `PowerSpectrum`: `freqs_hz[]`, `power_dbm[]`, `timestamp`. One sweep result.
- `Baseline`: per-bin `mean_dbm[]`, `std_dbm[]`, `sweep_count`, `freq_range`,
  `bin_hz`, `created_at`. The learned clean profile.
- `Candidate`: `center_freq_hz`, `bandwidth_est_hz`, `peak_power_dbm`,
  `snr_over_baseline_db`. A peak worth investigating.
- `Finding`: a `Candidate` plus `score` (0 to 100), `band_label`, ordered
  `reasons[]`, and a per-detector `breakdown`.

## 6. v1 workflow (sweep-now)

1. **Baseline.** Capture a clean RF profile, either of a reference space or of
   the same room at a trusted time. Stored as per-bin mean and standard deviation
   over N averaged sweeps. (Caveat surfaced in UI: a bug already transmitting
   during baseline capture will be learned as normal. Baselining a known-clean
   reference, or a quiet time, mitigates this.)
2. **Live sweep.** Run the same sweep now. The diff detector flags bins that
   exceed baseline by k standard deviations, or peaks absent from the baseline.
   Contiguous flagged bins are grouped into candidates.
3. **Analyze each candidate.**
   - `catalog`: label by band. A known broadcast / WiFi / cellular / ISM band
     lowers suspicion; an unexplained band raises it.
   - `signatures`: narrowband persistent carrier? near-field very strong (high
     absolute power)? in a band commonly used by covert transmitters? Each rule
     contributes a weighted reason.
   - `audio_corr` (on demand per candidate): tune to the candidate, demodulate
     its envelope (AM and FM), play a test chirp through the laptop speaker while
     recording the microphone, and correlate the RF envelope against the emitted
     and captured audio. High correlation strongly implies a live audio bug.
4. **Score and rank.** `score.py` fuses detector outputs into a 0 to 100
   suspicion score per candidate with ordered, human-readable reasons (for
   example: "not in any known band", "narrowband carrier", "envelope correlates
   with room audio at 0.82").
5. **Report.** Results shown live in the web UI (waterfall plus ranked suspect
   table) and exportable as JSON plus a readable summary.

## 7. Detection scoring

Each detector returns a weighted contribution plus reason text. Weights live in a
single tunable config block so they can be calibrated against real sweeps.
Initial weighting intent:

- **Audio-correlation** is the strongest positive signal (a confirmed
  envelope-to-room-audio correlation is close to dispositive for a live bug).
- **Unexplained band + narrowband + persistent** together form a strong combo.
- **Known-band match** is a strong negative that reduces the score (a strong
  signal sitting exactly on a licensed broadcast or WiFi channel is expected).

A configurable threshold determines what is surfaced as a flagged finding;
everything is ranked regardless so the operator can scan the full list.

## 8. Radio layer (radio/)

- `device.py`: an abstract `Device` interface with `sweep(start_hz, stop_hz,
  bin_hz)` returning a stream of `PowerSpectrum`, and `capture_iq(center_hz,
  sample_rate, duration)` returning IQ, plus bias-tee control. Pluggable so a
  second SDR can be added later without touching the engine.
- `hackrf.py`: implements the interface against the HackRF One. The sweep path
  uses HackRF wideband sweep; the IQ path captures tuned IQ. External SDR
  binaries are invoked with fixed argument lists (no shell interpolation), and
  all frequency and sweep parameters are validated before use.

## 9. Audio layer (audio/)

`capture.py` emits a known test signal (a chirp or tone sequence) through the
system speaker and records the microphone via the `sounddevice` library,
providing time-aligned audio buffers for the correlation detector.

## 10. Web UI (web/)

- Live waterfall display of the current sweep.
- Ranked suspect table: score, frequency, band label, reasons, and an
  "investigate" button that triggers the audio-correlation test on demand for a
  selected candidate.
- Baseline controls: capture baseline, load baseline, show baseline age.
- Phase 3 adds a locator view: a large live RSSI meter and a rising
  geiger-counter audio tone for hot/cold homing.

## 11. Error handling

- No HackRF connected: clear error, plus a demo mode driven by a recorded sweep
  so the UI and engine can be exercised without hardware.
- USB or sweep failures: surfaced to the UI and retried.
- No audio input/output device: the audio-correlation test is disabled
  gracefully with a visible notice; all other detectors continue.

## 12. Testing strategy

The engine separation exists precisely to make this possible:

- **Engine unit tests** against recorded and synthetic spectra. Fixtures include:
  a baseline-identical sweep (expect zero findings); a sweep with a planted
  narrowband carrier in an unexplained band (expect a high-score finding); a
  strong signal sitting on a known WiFi or FM channel (expect a low score); an IQ
  fixture whose envelope correlates with a reference audio track (expect the
  audio detector to fire).
- **Radio layer tests** against recorded captures and mocked subprocess output.
- **Catalog tests** verifying frequency-to-band labeling at band edges.

## 13. Stack

Python 3.11+, uv, hatchling, Flask + flask-socketio, numpy, scipy (matching the
Aetherscope toolchain), plus `sounddevice` for the audio-correlation test and
SigMF for recordings and fixtures.

## 14. Legal and ethical scope

Documented in README.md and SECURITY.md. Quietroom is receive-only: it does not
transmit, jam, or intercept communications content. The audio-correlation test
compares signal envelopes only; it does not demodulate or record communications.
It is intended for sweeping spaces the operator owns or is authorized to sweep.

## 15. Phase breakdown for implementation

- **Phase 1 (v1):** data types, baseline build/compare, the four detectors, the
  scoring fusion, the HackRF sweep + IQ radio layer, the audio layer, the web UI
  for sweep-now, demo mode, and the engine test suite.
- **Phase 2:** continuous monitor loop, new-signal alerting, persistence
  tracking, Socket.IO push notifications.
- **Phase 3:** locator view with live RSSI meter and audio homing tone.
