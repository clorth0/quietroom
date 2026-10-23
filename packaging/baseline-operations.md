# Baselines: refresh policy, anchor, and operator commands

A monitoring node scores every sweep against a learned baseline of its room.
`[baseline]` in the node config controls when that baseline is refreshed
(max age, drift, front-end change). This page is the runbook for the safety
rails around refreshing and the commands an operator uses on them.

## The two rules

1. **Never learn an active incident.** An alert-grade finding (score >=
   `freeze_score`, default the alert threshold) holds learning on that segment
   for `incident_hold_s` after the emitter's last hit. A refresh whose own
   capture sweeps show an alert-grade emitter is discarded.
2. **Never drift away from the anchor unseen.** Each segment has an *anchor*:
   a long-lived reference baseline per front end (radio, gains, antenna, sweep
   plan) that refreshes never move. Every refresh candidate is scored against
   the anchor with the live detector; an alert-grade difference is held like an
   incident, sent as an `anchor_divergence` event (fleet + alert channels) and
   shown under `anchors.<segment>.held_candidate` in health.

Rule 2 is what stops *slow creep*: an emitter raised a few dB per refresh
period never looks like a step to the live detector, and without an anchor each
refresh would learn it. Against the anchor the cumulative change is visible.

The first baseline captured under a front end becomes its anchor. A node
upgraded from a release without anchors adopts the **earliest** stored baseline
for its current front end (logged as `migrated: ...`), so creep that happened
before the upgrade is caught too. Swapping the front end back to an earlier
configuration reuses that configuration's anchor.

## Operator commands

Run on the node as the service user (`sudo -u quietroom quietroom-node --config
/etc/quietroom/config.toml ...`). Commands are queued in the node's store and
applied by the running service on its next tick (it owns the radio). Every
request, its outcome and the operator's `--note` are kept in the store
(`control_requests`) and sent to central as a `control` event.
Add `--wait 120` to block for the outcome (exit 0 done, 1 failed, 2 refused,
3 not picked up yet).

| Situation | Command |
|---|---|
| Sweep team removed the device; don't wait out the hold | `--ack SEG --freq 308M --note "TSCM-123 removed"` |
| Several emitters, all resolved | `--ack SEG` (no `--freq`) |
| Room legitimately changed (furniture, new partition), no incident | `--rebaseline SEG --note "..."` |
| New *legitimate* transmitter keeps alerting | `--rebaseline SEG --force --note "..."`, then `--promote-anchor SEG` |
| Anchor divergence was investigated and is legitimate | `--promote-anchor SEG --note "..."` |

* `--ack` releases only hits seen **up to the time you ran it**. If the
  emitter is still on air, the next sweep freezes learning again: acknowledging
  is "it's gone", not "it's fine". To keep something that is fine, re-baseline
  with `--force` and promote the anchor.
* `--rebaseline` without `--force` is **refused** (not deferred) while an
  incident holds the segment, and refused if the capture itself diverges from
  the anchor or shows an alert-grade emitter.
* `--rebaseline --force` learns whatever is on air, prints a warning, and
  records `forced`, the hold it overrode and the alert-grade emitters it learned
  over in the baseline's version detail. The anchor is not moved: the forced
  baseline still shows as divergence and automatic refreshes that carry it stay
  held until `--promote-anchor`.
* `--promote-anchor` makes the baseline currently in use the anchor (append-only
  history in `baseline_anchors`). The emitters that separated it from the old
  anchor are thereby accepted: their incident holds (live, capture and anchor
  hits up to the request) end too, and the release survives a restart.

## What to watch in health / heartbeat

* `incident_hold.<segment>`: emitters holding learning and until when.
* `rebaseline_deferred.<segment>`: a refresh that is waiting, and why.
* `anchors.<segment>`: anchor version and reason, `divergence` (current
  baseline vs anchor: non-empty only after a forced re-baseline that was not
  promoted), `held_candidate` (refresh refused against the anchor), and
  `drift` / `broadband_drift` (whole-band movement vs the anchor, informational).
