# Judgment calls

## Call 1. Sweep span

Use `SweepPlan.search` when the sweep should resolve a narrow carrier. Search stays at the 25 kHz default. Use `SweepPlan.overview` when the operator asked for a bin wider than the narrowband threshold. Commit `47fde9b` shipped a 1 MHz default, and the narrowband signature never fired. `tests/test_plan.py` fails if search accepts that wide bin.

## Call 2. Capture words

Use `CaptureFailed`, the words `capture failed`, and exit 1 when the radio does not return the IQ that was asked for. Use `NoAudioMatch` and the sentence `not a detectable live audio bug` when a full capture has no audio correlation. That path exits 0. Commit `2c4e21e` is where a failed IQ capture stopped reading as a clean room. `tests/test_cli_investigate.py` fails if a dead capture prints the clean sentence.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for `clorth0/quietroom` via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels are used verbatim (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
