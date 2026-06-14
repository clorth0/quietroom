# Quietroom

Receive-only RF bug-sweep tool. Quietroom drives a HackRF One to hunt for hidden
transmitters in a space by finding emitters that do not belong, then ranks them
by how suspicious they are and tells you why in plain language.

It is a technical surveillance countermeasures (TSCM) aid for sweeping spaces you
own or are authorized to sweep. See [SECURITY.md](SECURITY.md) for scope and
intended use.

## Status

Early development. The detection engine (baseline differencing, four-detector
scoring, ranked findings) is implemented and tested. The radio, audio, and web
layers are next.

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

## License

MIT. See [LICENSE](LICENSE).
