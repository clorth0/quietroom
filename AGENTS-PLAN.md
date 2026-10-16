# Make Quietroom safe for the next agent

Five stacked PRs turn the mistakes this repo keeps repairing into build failures. The next agent is who this protects. The maintainer inherits checks that name the fix, and two short judgment calls in `AGENTS.md`.

Structure comes first, then types, then a lint or a test. Prose is only for a call a check cannot encode. The owner approves this plan before execution. This document does not change production code.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. A nested box is a sub-step of the box above it. Check a box only when its evidence exists, a file, a log line, a screenshot, a test run, or a SHA. The body is a how-to. The appendices explain and record.

The program runs `pstack/skills/poteto-mode/playbooks/autopilot-stack.md`. The operator lands every PR. Owners stop at STACK-READY. No owner merges.

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [ ] State the protocol and this plan to the operator, then stop. Start execution only on the operator's explicit go.
- [ ] Read these from trunk at program start. Re-read them at every tick.
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/autopilot-stack.md`
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/autopilot-full.md`
  - [ ] `git show origin/main:pstack/skills/swarm/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/poteto-mode/playbooks/opening-a-pr.md`
  - [ ] `git show origin/main:pstack/skills/how/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/interrogate/SKILL.md`
  - [ ] `git show origin/main:pstack/skills/show-me-your-work/SKILL.md`
- [ ] If `git show` cannot find those paths, Quietroom does not vendor pstack. Read the plugin copies named in Appendix D and record that fallback in the decision log.
- [ ] On the operator's go, arm the audit tick as `/loop 1h` with the tick prompt below. Never leave the cadence to memory.
- [ ] Use this tick prompt, verbatim.

```
Re-read the execution playbook from trunk. Audit the operation against it and fix drift in this tick. Probe every active lane and judge progress by side effects only. Stand down a stuck lane and dispatch its replacement now. Then post a short status message to the operator in chat only when the audit found a tracked change that no earlier status message reported, such as a PR opened, a code-ready head, a round launched or closed, a verdict, a merge, a stuck agent and the action taken, a blocker added or cleared, or a decision only the operator can make. Name every such change and nothing else. Do not repeat a table, the merged list, or an unchanged blocker. If the audit found none, end the turn with no reply text. Either way, log this tick's row in your decision trail. The row names the items reported, or none.
```

- [ ] On the operator's hold or stand-down, send every owner a zero-writes order at once.

### Spawn owners

- [ ] Spawn one owner per PR with the full lifecycle the execution playbook names.
- [ ] Follow this dependency graph. Start dependent work only after its parent exists, and base it on the parent branch.
  - [ ] PR-1 branches from `main`.
  - [ ] PR-2 after PR-1.
  - [ ] PR-3 after PR-2.
  - [ ] PR-4 after PR-3.
  - [ ] PR-5 after PR-4.
- [ ] Hold the file boundaries. PR-1 touches `quietroom/engine/plan.py`, `quietroom/cli.py`, `quietroom/web/session.py`, `quietroom/web/app.py`, `quietroom/radio/recorded.py`, `quietroom/engine/pipeline.py`, `quietroom/engine/spectrum.py`, `quietroom/engine/persistence.py`, `README.md`, and the tests those files force to change. PR-2 touches `quietroom/radio/device.py`, `quietroom/radio/hackrf.py`, `quietroom/radio/parse.py`, `quietroom/audio/capture.py`, `quietroom/cli.py`, `quietroom/scan.py`, and the tests those files force to change. PR-3 touches `quietroom/web/commands.py`, `quietroom/web/app.py`, and `tests/test_web_commands.py`. PR-4 touches `quietroom/web/bind.py`, `quietroom/web/app.py`, and `tests/test_web_security.py`. PR-5 touches `AGENTS.md` only.
- [ ] Hold the review gate. PR-1, PR-2, and PR-3 change an interaction. They wait for the operator's review in chat with screenshots and a video before the operator lands them.

### PR mechanics, for every PR

- [ ] Resolve the forge once. Default to `gh`. If `command -v origin` succeeds and Origin can resolve the repository, use `origin pr` for every PR operation. Record any fallback to `gh`. Never require `gt`.
- [ ] Open the PR ready, never draft, per Opening a PR. Use the run's built-in PR tool when it has one, else `origin pr create --status open --base <base-branch>` or `gh pr create --base <base-branch>` according to the resolved forge. A stack child targets its parent branch.
- [ ] Run `uv run ruff check .` and `uv run mypy` once before the PR-facing push. Push with hooks on.
- [ ] Run `/deslop` before each commit and `/no-comments` before review. If those skills are not installed, say so in the PR body and still reread the diff for narration comments and filler.
- [ ] Triage every Bugbot and security-reviewer comment per `pstack/skills/poteto-mode/references/bugbot-triage.md`.
- [ ] Rebase onto the parent branch before the code-ready report and babysit. Keep that merge base in fix rounds. Rebase again only at merge prep, on a `git merge-tree` conflict with the parent, or on a CI failure that comes from a change on the parent.

### Verdict and merge, for every PR

- [ ] A full swarm runs once, at the code-ready head. Another full swarm runs only for new behavior, a redesign, or a large new diff. A rebase, a merge-conflict resolution, or a minor fix gets a single verifier at the new head. Run each full swarm per `pstack/skills/swarm/SKILL.md`. One gates lane. The ten live lanes from the PR's Verify, live block. The perf lane from its Verify, perf block. Two or more audit lanes, each with its own focus, that read the diff and the receipts and distrust the PR body. The root audits the receipts in the STACK-READY report before the verdict.
- [ ] Clean only when every lane is `PASS`. Findings go back to the owner, including a defect that a lane filed as a note. A new head gets a fresh verdict, except for results that stay valid under the patch-id rule in `pstack/skills/poteto-mode/playbooks/shipping.md`.
- [ ] The owner reports STACK-READY and does not merge, retarget, or arm auto-merge. The root sets each child base to its parent branch. The operator lands the stack bottom-up after the review gates.

### Boot recipe, for every live lane

Each live lane runs on its own cloud VM at the PR head. This repo has no `control-cli` and no `control-ui` skill. Appendix C records that. Drive the CLI with `uv` and the web UI with `curl` plus a one-shot Socket.IO client. Save the terminal as a png.

- [ ] `git fetch origin <head-branch> && git checkout <head SHA>`.
- [ ] `uv sync --locked`. For a web lane, start `uv run quietroom-web --demo --port 8770` and wait until `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8770/` prints `200`.
- [ ] Deliver input only through `uv run quietroom`, `curl`, and a Socket.IO client. Read-only diagnostics are `curl -D -` and the process exit code.
- [ ] Save every screenshot to `/tmp/swarm-<pr-id>/worker-<n>/<slug>.png` and return the paths with the report.

## Give sweep settings one owner (PR-1)

**Depends on.** None.

**Files.**

- [ ] Create `quietroom/engine/plan.py`.
- [ ] Edit `quietroom/cli.py`, `quietroom/web/session.py`, `quietroom/web/app.py`, `quietroom/radio/recorded.py`, `quietroom/engine/pipeline.py`, `quietroom/engine/spectrum.py`, `quietroom/engine/persistence.py`, and `README.md`.
- [ ] Edit the tests that construct `Finding` once `raw_score` has no default.

**Build.**

- [ ] Add `SweepPlan` in `quietroom/engine/plan.py`. `SweepPlan.search` is the default. It rejects a bin wider than `NARROWBAND_HZ`. `SweepPlan.overview` accepts a coarse bin. The span, the bin, `MIN_HZ`, and `MAX_HZ` each live in one name. `cli.py`, `session.py`, and `app.py` call that name.
- [ ] Make `pipeline.sweep_findings` raise when the spectrum's bin spacing does not match the plan. Change `DemoDevice` and `StreamingDemoDevice` so the grid they build uses the requested `bin_hz`.
- [ ] Make `Finding.raw_score` required. Delete the `unsquash` fallback in `persistence._annotate`. Update the README example so it matches the new demo line.

**You see.**

- [ ] `uv run quietroom --demo` prints `308.000` and `narrowband carrier`, and the SCORE column is `86`. Trunk prints `84` and `1000 kHz`.
- [ ] `SweepPlan.search` with bin `1_000_000` raises, and the message names `overview` and the narrowband threshold.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_plan.py` gains the search rejection, the overview acceptance, and the spacing mismatch. Run `uv run pytest tests/test_plan.py tests/test_cli.py tests/test_score.py tests/test_catalog.py -q`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-xhigh-fast` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run `uv run quietroom --demo` at trunk and at head. Trunk lacks a demo grid that honors 25 kHz, so record the trunk line with `1000 kHz` and score `84`, and gate the head line plus a process exit of 0. Save `pr1-lane-1.png`. Pass when head prints `308.000`, `narrowband carrier`, and score `86`, and trunk still prints score `84`.
- [ ] Lane 2. Run a one-shot that constructs `SweepPlan.search(300_000_000, 320_000_000, 1_000_000)`. Save `pr1-lane-2.png`. Pass when it raises and the text names `overview`.
- [ ] Lane 3. Run a one-shot that constructs `SweepPlan.search` at `25_000`. Save `pr1-lane-3.png`. Pass when it returns a plan and does not raise.
- [ ] Lane 4. Run a one-shot that constructs `SweepPlan.overview` at `1_000_000`. Save `pr1-lane-4.png`. Pass when it returns a plan and does not raise.
- [ ] Lane 5. Run `uv run quietroom --demo -v`. Save `pr1-lane-5.png`. Pass when the breakdown contains `raw` and the process exits 0.
- [ ] Lane 6. Run `rg -n "300_000_000|320_000_000" quietroom`. Save `pr1-lane-6.png`. Pass when the only hit is `quietroom/engine/plan.py`.
- [ ] Lane 7. Run `rg -n "MIN_HZ|MAX_HZ" quietroom`. Save `pr1-lane-7.png`. Pass when the assignments live in `quietroom/radio/hackrf.py` and other hits are imports.
- [ ] Lane 8. Run a one-shot that asks `demo_device().sweep` for bin `25_000` and prints the spacing. Save `pr1-lane-8.png`. Pass when the spacing is `25000`.
- [ ] Lane 9. Run `uv run pytest tests/test_catalog.py -q`. Save `pr1-lane-9.png`. Pass when a near-field emitter in a shared band still contributes `0` catalog points.
- [ ] Lane 10. Run `uv run pytest tests/test_score.py -q`. Save `pr1-lane-10.png`. Pass when strong suspects stay ordered and every score is below `100`.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall time of `uv run quietroom --demo`, median of 5 runs, at trunk and at head. Also the time to print the `308.000` line, which is the end state the operator waits for.
- [ ] Probe. Run the command 5 times at trunk, then 5 times at head, then 5 times at trunk again. Record each median.
- [ ] Baseline. Record the trunk median first. On the plan-writing VM that median was `0.771` seconds on 2026-10-16. The lane VM records its own trunk number and uses that.
- [ ] Rule. Fail when the head median is above `1.25` times the trunk median, or above `3` seconds.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 1 screenshots into `docs/review/pr-1-review-demo.png`.
- [ ] Record a 30 to 60 second video of `uv run quietroom --demo` on a lane VM. Save it as `docs/review/pr-1-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto current trunk after the verdict, patch-id unchanged.
- [ ] The owner reports STACK-READY. The operator lands PR-1.

## Make a failed capture a different result from a quiet room (PR-2)

**Depends on.** PR-1.

**Files.**

- [ ] Create `quietroom/radio/capture.py` with `IqCapture` and `require_iq`.
- [ ] Edit `quietroom/radio/hackrf.py`, `quietroom/radio/device.py`, `quietroom/audio/capture.py`, `quietroom/cli.py`, `quietroom/scan.py`, and the tests those files force to change.
- [ ] Delete the `audio_test` parameter of `scan.investigate`. Do not leave a wrapper that still accepts a float.

**Build.**

- [ ] `require_iq` raises `DeviceError` unless the array length equals the requested length. `HackRFDevice.capture_iq` checks the process return code and then calls `require_iq`. `audio_correlation_test` builds an `IqCapture` and returns `LiveBug`, `NoAudioMatch`, or `CaptureFailed`.
- [ ] `format_verdict` accepts that result and does not accept a float. `CaptureFailed` prints `capture failed` and `main` exits `1`. `NoAudioMatch` keeps the no-correlation sentence and exits `0`.
- [ ] `confirmed_findings` raises when the device yields fewer sweeps than requested. Delete the `min()` that lowers `min_hits`.

**You see.**

- [ ] `uv run quietroom --demo --investigate 308M` still prints `LIKELY LIVE BUG`.
- [ ] A device that returns no samples prints `capture failed` and does not print `not a detectable live audio bug`.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_web_device_errors.py` and `tests/test_investigate.py` cover the dead process, the empty array, the short array, and a real chirp. Run `uv run pytest tests/test_web_device_errors.py tests/test_investigate.py tests/test_audio_correlation_test.py tests/test_lag_correlation.py -q`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-xhigh-fast` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run `uv run quietroom --demo --investigate 308M` at trunk and at head. Save `pr2-lane-1.png`. Pass when both print `LIKELY LIVE BUG` and exit `0`.
- [ ] Lane 2. Run the dead-process case from `tests/test_web_device_errors.py` through the CLI helper. Save `pr2-lane-2.png`. Pass when the text contains `capture failed`, the exit code is `1`, and the text does not contain `not a detectable live audio bug`.
- [ ] Lane 3. Run a device whose `capture_iq` returns an empty array. Save `pr2-lane-3.png`. Pass when the result is `CaptureFailed` and the clean phrase is absent.
- [ ] Lane 4. Run a device whose `capture_iq` returns 8 samples when 1000 were asked. Save `pr2-lane-4.png`. Pass when the result is `CaptureFailed`.
- [ ] Lane 5. Run a full buffer of unrelated noise through `format_verdict`. Save `pr2-lane-5.png`. Pass when the sentence says there is no audio correlation and the exit code is `0`.
- [ ] Lane 6. Run `uv run pytest tests/test_lag_correlation.py -q`. Save `pr2-lane-6.png`. Pass when a half-sample offset still scores above `0.95`.
- [ ] Lane 7. Run `confirmed_findings` with `n_sweeps=3` and `min_hits=2` against a device that yields one hot sweep. Save `pr2-lane-7.png`. Pass when it raises and reports no finding.
- [ ] Lane 8. Run a one-shot that constructs `Finding` without `raw_score`. Save `pr2-lane-8.png`. Pass when construction raises.
- [ ] Lane 9. Run `rg -n "audio_test" quietroom tests`. Save `pr2-lane-9.png`. Pass when there are no hits.
- [ ] Lane 10. Run the web investigate error test with a device that raises. Save `pr2-lane-10.png`. Pass when the socket emits `error` and does not emit `finding`.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall time of `uv run quietroom --demo --investigate 308M`, median of 5 runs, at trunk and at head. Also the time until `LIKELY LIVE BUG` is printed.
- [ ] Probe. Run the command 5 times at trunk, then 5 times at head, then 5 times at trunk again.
- [ ] Baseline. Record the trunk median first on the lane VM.
- [ ] Rule. Fail when the head median is above `1.25` times the trunk median, or above `5` seconds.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 2 screenshots into `docs/review/pr-2-review-failed-capture.png`.
- [ ] Record a 30 to 60 second video of the failed-capture line and the demo investigate line on a lane VM. Save it as `docs/review/pr-2-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto PR-1 after the verdict, patch-id unchanged.
- [ ] The owner reports STACK-READY. The operator lands PR-2 after PR-1.

## Parse every socket event in one place (PR-3)

**Depends on.** PR-2.

**Files.**

- [ ] Create `quietroom/web/commands.py`.
- [ ] Edit `quietroom/web/app.py`.
- [ ] Create `tests/test_web_commands.py`.

**Build.**

- [ ] `parse_command(event, data)` returns a command or an error string. `dispatch` is the only function a socket handler calls. Unknown events, bad numbers, and device failures become `error` payloads inside `dispatch`.
- [ ] Replace each `@socketio.on` body with a call to `dispatch`. Accept `cycles` from `1` to `30`. Any other value emits `error`. When `start` has no session, emit `error` with `no device`.
- [ ] Add an AST test that fails when a `@socketio.on` function's body is not a single `dispatch` call. The failure text names `quietroom.web.commands.dispatch`.

**You see.**

- [ ] A non-numeric `cycles` value emits `error` and the status line does not stay on `capturing baseline`.
- [ ] `start` with no session emits `no device`. Trunk returns without an event.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_web_commands.py` covers cycles, frequency, region, the no-session start, and the AST rule. Run `uv run pytest tests/test_web_commands.py tests/test_web_socket.py tests/test_web_device_errors.py -q`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-xhigh-fast` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Open `http://127.0.0.1:8770/` with `quietroom-web --demo` at trunk and at head, and emit `capture_baseline` with `cycles` of `3`. Save `pr3-lane-1.png`. Pass when both replies are `baseline_ready`.
- [ ] Lane 2. Emit `capture_baseline` with `cycles` of `nope`. Save `pr3-lane-2.png`. Pass when the reply is `error` and the message says `invalid cycles`.
- [ ] Lane 3. Emit `investigate` with `freq_hz` of `nope`. Save `pr3-lane-3.png`. Pass when the reply is `error` and the message says `invalid frequency`.
- [ ] Lane 4. Emit `set_region` with region `zz`. Save `pr3-lane-4.png`. Pass when the reply is `error` and the message names the known regions.
- [ ] Lane 5. Emit `investigate` with `freq_hz` of `1000`. Save `pr3-lane-5.png`. Pass when the reply is `error` and the message says the frequency is out of range.
- [ ] Lane 6. Call `start` on an app whose session is `None`. Save `pr3-lane-6.png`. Pass when head emits `no device`. Record that trunk emits nothing.
- [ ] Lane 7. Emit `capture_baseline` with `cycles` of `0`. Save `pr3-lane-7.png`. Pass when head emits `error` and does not emit `baseline_ready`. Record that trunk runs one sweep.
- [ ] Lane 8. Emit `capture_baseline` on a device that raises `DeviceError`. Save `pr3-lane-8.png`. Pass when the message starts with `baseline failed`.
- [ ] Lane 9. Run `uv run pytest tests/test_web_commands.py -q` against head, and run the same AST check on `git show 2c4e21e^:quietroom/web/app.py`. Save `pr3-lane-9.png`. Pass when head passes and the old file fails.
- [ ] Lane 10. Load the demo page and confirm the region dropdown still lists `us` and `eu`. Save `pr3-lane-10.png`. Pass when both options are present and `us` is selected by default.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Time from process start until `curl` gets `200` from `quietroom-web --demo`, median of 5 runs, at trunk and at head. Also the time from the `capture_baseline` emit until `baseline_ready`.
- [ ] Probe. Start the server 5 times at trunk and 5 times at head, interleaved. Time each ready event with the same client script.
- [ ] Baseline. Record the trunk medians first on the lane VM.
- [ ] Rule. Fail when the head ready median is above `1.25` times the trunk median, or when `baseline_ready` takes above `2` seconds on the demo device.

**Review gate.** The operator reviews before merge.

- [ ] Copy lane 6 and lane 7 screenshots into `docs/review/pr-3-review-errors.png`.
- [ ] Record a 30 to 60 second video of the demo page receiving an error for `cycles` of `0` and then a normal baseline. Save it as `docs/review/pr-3-review.mp4`.
- [ ] Post the screenshots and the video in chat. Stop at STACK-READY. Wait for the operator.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto PR-2 after the verdict, patch-id unchanged.
- [ ] The owner reports STACK-READY. The operator lands PR-3 after PR-2.

## Make the bind policy unable to disable its own check (PR-4)

**Depends on.** PR-3.

**Files.**

- [ ] Create `quietroom/web/bind.py`.
- [ ] Edit `quietroom/web/app.py`.
- [ ] Edit `tests/test_web_security.py`.

**Build.**

- [ ] Add `LocalBind` and `RemoteBind`. `RemoteBind` requires a token of at least 16 characters and refuses a non-loopback host without that token. `create_app` takes a bind value and does not take an origins list.
- [ ] Pass a callable to `SocketIO`, built from the bind value. Add an AST test that fails when `cors_allowed_origins` is a list, a string, or `[]`. The failure text says to pass the callable from `quietroom.web.bind`.
- [ ] Keep the current loopback, Host-header, and token behavior. Do not add a new remote mode.

**You see.**

- [ ] `uv run quietroom-web --demo` still answers `200` on `127.0.0.1` with no token.
- [ ] `uv run quietroom-web --host 0.0.0.0` still refuses to start, with the same refusal text trunk prints.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] `tests/test_web_security.py` gains the AST case and keeps the origin, Host, and token cases. Run `uv run pytest tests/test_web_security.py -q`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-xhigh-fast` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run `curl -D - http://127.0.0.1:8770/` against `quietroom-web --demo` at trunk and at head. Save `pr4-lane-1.png`. Pass when both return `200` and both send `Content-Security-Policy`.
- [ ] Lane 2. Run `uv run quietroom-web --host 0.0.0.0`. Save `pr4-lane-2.png`. Pass when the process exits non-zero and the text matches trunk.
- [ ] Lane 3. Run `uv run quietroom-web --host 0.0.0.0 --allow-remote` with `QUIETROOM_TOKEN` unset. Save `pr4-lane-3.png`. Pass when the process exits non-zero and mentions the token.
- [ ] Lane 4. Run the same command with a 15 character token, then with a 16 character token, bound on the lane VM. Save `pr4-lane-4.png`. Pass when the 15 character token exits non-zero and the 16 character token returns `200` from curl.
- [ ] Lane 5. Run the AST check on head and on `git show bfacdd3^:quietroom/web/app.py`. Save `pr4-lane-5.png`. Pass when head passes and the old file fails on the list.
- [ ] Lane 6. Connect a socket with `Origin` set to `http://evil.example`. Save `pr4-lane-6.png`. Pass when the connection is rejected.
- [ ] Lane 7. `curl -H 'Host: evil.example' http://127.0.0.1:8770/`. Save `pr4-lane-7.png`. Pass when the status is `403`.
- [ ] Lane 8. `curl http://127.0.0.1:8770/` with no token. Save `pr4-lane-8.png`. Pass when the status is `200`.
- [ ] Lane 9. Run `rg -n "cors_allowed_origins" quietroom`. Save `pr4-lane-9.png`. Pass when the only hit is the `SocketIO` call and the argument is a name, not a list.
- [ ] Lane 10. Load `/` and confirm the page title and the waterfall canvas are still there. Save `pr4-lane-10.png`. Pass when the HTML contains the canvas element trunk serves.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Time from process start until `curl` gets `200` from `quietroom-web --demo`, median of 5 runs, at trunk and at head.
- [ ] Probe. Interleave 5 trunk starts and 5 head starts. Use the same port and the same curl.
- [ ] Baseline. Record the trunk median first on the lane VM.
- [ ] Rule. Fail when the head median is above `1.25` times the trunk median, or above `3` seconds.

**Review gate.** None. PR-4 is not review-gated.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto PR-3 after the verdict, patch-id unchanged.
- [ ] The owner reports STACK-READY. The operator lands PR-4 after PR-3.

## Write the two judgment calls (PR-5)

**Depends on.** PR-4.

**Files.**

- [ ] Create `AGENTS.md`.
- [ ] Do not edit Python, tests, or CI in this PR.

**Build.**

- [ ] Write two calls. Each call names the type that enforces it and the commit that showed the failure. The first call says when a span is `SweepPlan.search` and when it is `SweepPlan.overview`. The second call says when the words are `capture failed` and when they are the no-correlation sentence.
- [ ] Point each call at the test that fails if the type is bypassed. Do not add a third call.

**You see.**

- [ ] `AGENTS.md` exists on head and does not exist on trunk.
- [ ] `uv run quietroom --demo` still exits `0` after the file is added.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Do not add a test that restates `AGENTS.md`. Run `uv run pytest -q` and confirm the suite still exits `0`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `grok-4.7-xhigh-fast` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run `uv run quietroom --demo` at trunk and at head. Trunk has no `AGENTS.md`. Save `pr5-lane-1.png`. Pass when both exit `0` and head prints `308.000`.
- [ ] Lane 2. Run `git cat-file -t 47fde9b` and `git cat-file -t 2c4e21e` from the repo. Save `pr5-lane-2.png`. Pass when both print `commit` and `AGENTS.md` contains both SHAs.
- [ ] Lane 3. Read the search call. Save `pr5-lane-3.png`. Pass when it names `SweepPlan` and a test file that exists.
- [ ] Lane 4. Read the capture call. Save `pr5-lane-4.png`. Pass when it names `CaptureFailed` and a test file that exists.
- [ ] Lane 5. Count the calls in `AGENTS.md`. Save `pr5-lane-5.png`. Pass when the count is `2`.
- [ ] Lane 6. Run `wc -l AGENTS.md`. Save `pr5-lane-6.png`. Pass when the file is under `40` lines.
- [ ] Lane 7. Run `uv run quietroom --demo -v`. Save `pr5-lane-7.png`. Pass when a raw total is still printed.
- [ ] Lane 8. Run `uv run quietroom --demo --investigate 308M`. Save `pr5-lane-8.png`. Pass when the line still says `LIKELY LIVE BUG`.
- [ ] Lane 9. Run `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8770/` against `quietroom-web --demo`. Save `pr5-lane-9.png`. Pass when the code is `200`.
- [ ] Lane 10. Run `uv run pytest -q`. Save `pr5-lane-10.png`. Pass when the suite exits `0`.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall time of `uv run quietroom --demo`, median of 5 runs, at trunk and at head.
- [ ] Probe. Interleave 5 trunk runs and 5 head runs.
- [ ] Baseline. Record the trunk median first on the lane VM.
- [ ] Rule. Fail when the head median is above `1.25` times the trunk median, or above `3` seconds. A markdown file has no extra work, so a miss here means the lane measured the wrong command.

**Review gate.** None. PR-5 is not review-gated.

**Merge.**

- [ ] Root's clean verdict at the exact head SHA.
- [ ] Bugbot triage done.
- [ ] Rebased onto PR-4 after the verdict, patch-id unchanged.
- [ ] The owner reports STACK-READY. The operator lands PR-5 after PR-4.

## Close the program

- [ ] Every box above is checked with its evidence.
- [ ] Reply to the operator with the STACK-READY report the execution playbook names.

## Appendix A. Prototype evidence

Commit `c39ab96` adds `prototypes/agent-safety/run.py`. The files are throwaway. `quietroom/` does not import them. Regenerate the receipt with `uv run python prototypes/agent-safety/run.py`. The committed receipt is `prototypes/agent-safety/last-run.json`.

A dead `hackrf_transfer` (return code `1`, no bytes) returns `0` samples and does not raise. An empty array and an 8-sample array both produce correlation `0.0`. `format_verdict` then prints `not a detectable live audio bug`. `IqCapture` rejects the empty array and the short array, and accepts a full buffer. A return-code check inside `HackRFDevice` alone leaves the other two cases in place. That is why PR-2 puts the length check on the value the verdict is built from, and deletes the float parameter.

`SweepPlan.search` at `1_000_000` raises and names `overview`. `SweepPlan.search` at `25_000` is accepted. `SweepPlan.overview` at `1_000_000` is accepted. A duplicated-literal scan of `47fde9b^` flags `1000000` on the CLI bin, the session bin, and `MIN_HZ` together, so that lint cannot say which one is wrong. The same scan at head still flags real copies. `300000000` and `320000000` are in `cli.py` and `session.py`. `1000000` and `6000000000` are in `hackrf.py` and `web/app.py`. PR-1 gives each value one owner. The literal lint is the wrong tool for the bin bug.

`demo_device().sweep` asked for bin `25000` returns spacing `1000000`. `uv run quietroom --demo` still prints `unresolved single-bin signal (<= 1000 kHz)` and score `84`. PR-1 makes the pipeline reject that spacing, so the demo grid has to change. The SCORE column becomes `86` because narrowband credit is `15` points and the unresolved credit is `7`. `squash(110)` is about `86.47`, which the table prints as `86`.

A `Finding` with score `100` and `raw_score` left as `None` annotates to raw `1023.137` and score `99.999999`. A non-clamped forgotten raw of `102` moves by about `3e-14`, which is not a defect. The defect is the optional field. PR-1 makes `raw_score` required and deletes the `unsquash` fallback.

`confirmed_findings` with `n_sweeps=3` and `min_hits=2` reports `1` finding when the device yields one hot sweep. The `min()` that causes this landed in `c3462c9`, the same commit that added the persistence bar. PR-2 makes a short sweep raise.

Head has `6` socket handlers and none of them delegate to one parser. `2c4e21e^` has the same shape on the handlers that existed then. PR-3 is that parser plus an AST test. `bfacdd3^` passes a list to `cors_allowed_origins`. Head passes the callable `_origin_ok`. In engineio `4.13.2`, `engineio/server.py` line `202` skips the origin check when the value is `[]`, and `engineio/base_server.py` line `321` says CORS handling is disabled for that value. PR-4 forbids a list at the only call site.

`gh api` returned `0` pull-request review comments and `0` issue comments on 2026-10-16. The evidence is the fix commits, not a review thread.

On this VM, 5 runs of `uv run quietroom --demo` had median wall time `0.771` seconds. The lane VM records its own trunk median. Do not reuse `0.771` as that baseline.

These questions stay open because a run cannot settle them. The operator may want `CaptureFailed` to print a traceback. PR-2 prints one line and exits `1`. The operator may want a coarse bin as the default for every span. PR-1 keeps `search` at `25_000` as the default, and `overview` is an explicit argument.

## Appendix B. Alternatives rejected

A duplicated-literal lint as the fix for the `1_000_000` bin. The same number is `MIN_HZ`. The message cannot name `SweepPlan`.

A process-return-code check only in `HackRFDevice.capture_iq`. The prototype's empty array and short array never start a process, and both still print a clean room.

Leaving `audio_test` in place so old tests keep injecting `0.9`. That parameter is how `tests/test_investigate.py` stays green when the capture path is wrong. PR-2 deletes it in the same change that adds the verdict.

A new package of scenario tests after the types already reject the bad values. The tests in PR-1, PR-2, PR-3, and PR-4 are the ones that call the public functions. A second suite that restates the same types does not earn a PR.

An `AGENTS.md` written first, with no types. Nothing fails when an agent skips the file. PR-5 is last, and it holds two calls.

Clamping `cycles` of `0` to `1`. Trunk does this in `web/app.py`. It hides a bad payload. PR-3 rejects it.

## Appendix C. Risks

PR-1 changes the demo sentence and the printed score from `84` to `86`. The operator reviews that before landing. A test that still expects `1000 kHz` will fail until it expects `narrowband carrier`.

PR-2 changes a dead radio from a clean sentence to `capture failed` and exit `1`. The operator reviews the wording. A caller outside this repo that passed `audio_test` will break. The package is the only caller today.

PR-3 rejects `cycles` of `0` and emits `no device` from `start`. The operator reviews the page. A client that sent `0` and relied on the clamp will see an error.

PR-4 can lock the operator out if `RemoteBind` is stricter than today's `main`. The live lanes compare the refusal text to trunk. If the text differs, the owner stops.

engineio owns the empty-list behavior. A later engineio could change it. The AST test still forbids a list in this repo, so the dependency change does not reopen the call site.

This checkout has no `control-cli` and no `control-ui` skill. Live lanes drive `uv`, `curl`, and a Socket.IO client, and they save the terminal as a png. If a control skill appears later, use it without changing the pass predicates.

`git show origin/main:pstack/...` fails in this repo. Appendix D has the plugin paths. The tick still re-reads those files.

`SweepPlan.overview` can silence the narrowband guard. That is the residual judgment call in PR-5. The default path does not pass it.

## Appendix D. Links and reading list

Read `prototypes/agent-safety/README.md` and run the prototype before editing. Read `SECURITY.md` before PR-4. Read `quietroom/radio/hackrf.py`, `quietroom/web/app.py`, `quietroom/audio/capture.py`, and `quietroom/engine/score.py` before the PR that touches them.

PR-1 and PR-2 get `pstack/skills/how/SKILL.md` before editing, because they move ownership. PR-2 also gets `pstack/skills/interrogate/SKILL.md` on the verdict sketch, because a float and a result type were both real options and the prototype picked the result type. The other PRs do not need a second design pass.

Plugin copies used to write this plan, if trunk has no `pstack/` tree.

- `skills/poteto-mode/playbooks/autopilot-stack.md`
- `skills/poteto-mode/playbooks/autopilot-full.md`
- `skills/swarm/SKILL.md`
- `skills/poteto-mode/playbooks/opening-a-pr.md`
- `skills/how/SKILL.md`
- `skills/interrogate/SKILL.md`
- `skills/show-me-your-work/SKILL.md`

The decision trail is `prototypes/agent-safety/decision-log.tsv`, in the format from `skills/show-me-your-work/SKILL.md`. Append a row when a PR owner chooses a shape the plan did not name.

Commits cited in the pitfalls. `47fde9b` narrowband never fired at the default bin. `2ddaf93` moved the default to 25 kHz and left the demo grid at 1 MHz. `09c402a` stopped a flat catalog penalty from hiding shared-band bugs. `d7ce2f7` stopped adding persistence on top of a clamped score. `2c4e21e` stopped a failed IQ capture from reading as a clean room. `c039344` stopped a dead sweep thread from vanishing. `283ba91` wired a live radio that `create_app` had left as `None`. `c3462c9` added persistence and the `min()` that lowers it. `8f786e0` added the missing `cycles` check. `36abe41` stopped the demo from hard-coding correlation `0.85`. `1d47653` stopped integer-lag correlation from missing a fractional delay. `bfacdd3` stopped `cors_allowed_origins=[]` from disabling origin checks. `a1c2967` added gain checks that the radio PR had left as a follow-up.
