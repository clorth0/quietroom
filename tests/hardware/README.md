# Hardware-in-the-loop scaffolding

Without hardware (default): `tools/soak.py` runs the real pipeline against
the streaming demo device and `tests/test_soak_scaffold.py` verifies the
software loop; the hardware smoke test is skipped unless `QUIETROOM_HW=1`.

With hardware (Pi + HackRF/RTL-SDR on a self-hosted runner): dispatch
`.github/workflows/hardware.yml` manually. Bench checklist: probe detection,
per-segment sweep timing, USB reset recovery, thermal soak (24h+), RTL-SDR
gain/integration behavior, and alert-quality scoring on recorded scenarios.
None of this has been run yet — no SDR hardware is available in CI/dev.
