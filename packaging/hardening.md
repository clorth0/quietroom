# Deployment hardening checklist (nodes)

Implemented in code/packaging: systemd sandboxing + watchdog (packaging/systemd),
group-scoped SDR USB access (packaging/udev), HMAC-signed staged updates with
rollback (quietroom/update.py), secret-file permission checks
(`check_secret_file`).

Operator/site responsibilities until automated:
- Full-disk or data-partition encryption for /var/lib/quietroom (SQLite, keys).
- Per-node credentials: unique update key per node, and a per-node mTLS client
  certificate bound to the node UUID for central (packaging/fleet-mtls.md);
  the shared `QUIETROOM_CENTRAL_TOKEN` is a deprecated migration fallback
  (removal planned for 0.5.0, not before 2027-04-01). Revoke a node with
  central's `--deny-list` (no restart needed).
- Local web UI stays loopback-only, or behind TLS + individual accounts; never
  expose Werkzeug directly (see SECURITY.md).
- Read-only root filesystem where the OS supports it, with /var/lib/quietroom
  on a separate writable partition; disable password SSH; outbound-only egress
  to the central server and update host.
- Physical: locked/enclosed mounting; a missed heartbeat is treated as a
  node-down/security event, not proof of tampering.
- Updates: stage during a maintenance window, verify `current_version()`,
  keep one known-good `previous` release; test rollback quarterly.

## Config validation

The node refuses to start on a config it cannot trust. Every key is checked
against a schema (`quietroom/configschema.py`, tables in `quietroom/config.py`):
unknown keys (with a did-you-mean), wrong types (`enabled = "false"` is an
error, not `true`), out-of-range values (`poll_s` 0.1–60 so the systemd
watchdog keeps being fed, `0 <= min_score < 100`, `cooldown_s >= 0`, segment
`priority` 0–100), `webhook_url` other than `http(s)://host` (no `file://`, no
`user:pass@`), and secrets in the file. All problems are listed at once, by key,
and the daemon exits 2 without a traceback. The shipped unit sets
`RestartPreventExitStatus=2`, so systemd does not restart-loop on a bad config:
fix it, then `systemctl restart quietroom-node`. `--check-config` reports the same
problems in its JSON (`ok: false`, `config_errors: [{key, message}]`) and exits 1.
Valid-but-suspicious settings are warnings, at start-up and in `--check-config`:
a plain `http://` webhook (signed, but readable and replayable on the wire), and
a `poll_s` longer than an enabled segment's `interval_s`.

## Alert secrets

SMTP and webhook secrets are read from the environment only
(`QUIETROOM_SMTP_PASSWORD`, `QUIETROOM_WEBHOOK_SECRET`); the config loader
rejects a `password` key in `[alerts.email]`, `webhook_secret` in `[alerts]` and
`token` in `[fleet]` (without echoing the value). Under systemd put them in
`/etc/quietroom/secrets.env` (owner `root:quietroom`, mode `0640`), which the
unit loads via `EnvironmentFile=`. SMTP AUTH is refused without STARTTLS or
implicit TLS, STARTTLS is mandatory (not opportunistic) when enabled, and the
server certificate is verified (system trust, or `ca_file` for a private relay).

Check that the service's environment really carries what the config needs,
before an alert depends on it. `--check-config` lists every env secret as
required/recommended and present/missing (never the value), checks the fleet
mTLS files and certificate UUID without creating anything, and exits 1 on an
error. Load the env file in a shell rather than on the command line so the
values never appear in `ps`:

```sh
sudo -u quietroom sh -c 'set -a; . /etc/quietroom/secrets.env; exec \
  quietroom-node --config /etc/quietroom/config.toml --check-config'
```

On central, `quietroom-central --cert ... --key ... --client-ca ...
[--allow-legacy-token] [--deny-list FILE] [--cert-warn-days N] --check-config`
does the same for `QUIETROOM_CENTRAL_TOKEN`, `QUIETROOM_CENTRAL_READ_TOKEN`, the
TLS files, the deny list (exists, is UTF-8, every line a UUID; reports the count)
and `--cert-warn-days` (an integer from 1 to 365).

## Undeliverable alerts (dead letters)

Alerts queue per channel in `<store path>.alerts-outbox` and retry with
backoff (30 s doubling to 1 h), in order. An alert that has failed
`[alerts] max_attempts` times (default 5: retries over ~7.5 minutes) stops holding its
channel: newer alerts go out past it. It is *parked* (kept, never retried)
once it fails again after the channel has delivered other alerts since its
previous failure, i.e. when the message, not the relay, is the problem. A
relay outage therefore never parks anything. Independently, any alert
undelivered for `[alerts] max_age_s` (default 7 days) is parked as stale.

Parked alerts show in health and the fleet heartbeat as `alert_parked`
(per channel), and each parking sends an `alert_parked` event to central.
Inspect and retry them on the node, as the service user:

```sh
quietroom-node --config /etc/quietroom/config.toml --outbox list --parked
quietroom-node --config /etc/quietroom/config.toml --outbox requeue --id 12
quietroom-node --config /etc/quietroom/config.toml --outbox requeue --all --channel email
```

A requeued alert keeps its place in line with a fresh attempt budget and age;
the running node sends it on its next tick.

At most 500 alert sends are attempted per channel per tick (`ALERT_FLUSH_BATCH`,
mirroring the fleet outbox), so a long backlog drains over several ticks
instead of holding one tick past the watchdog.

## Fleet upload backlog

Uploads central has not accepted queue in `<store path>.fleet-outbox` and are
retried in order every tick, one network attempt per tick while central is
unreachable. Queued heartbeats coalesce (central keeps only the latest). The
queue is bounded the same way as alerts, plus a hard row limit so a node cut off
from central cannot fill its disk:

- `[fleet] outbox_max_rows` (default 20000, cannot be disabled): past this many
  queued rows the oldest are parked (`overflow`); past this many parked rows the
  oldest parked are deleted and counted (`fleet_outbox.dropped`). The file holds
  at most twice this many rows.
- `outbox_max_age_s` (default 7 days, 0 = never): older uploads are parked (`stale`).
- `outbox_max_attempts` (default 5, 0 = never): a row failing this often stops
  holding the queue, and is retried after the others; it is parked (`poison`)
  only if it fails right after central accepted another row. An outage alone
  never parks anything.

At most 500 rows go out per tick, so a long backlog drains over several ticks
instead of holding one tick past the watchdog. Requeued rows count toward
`outbox_max_rows` again, so requeueing more than that re-parks the oldest.

Health and the heartbeat carry `fleet_outbox: {pending, parked, dropped}`; each
parking is printed to the journal and emitted locally as a `fleet_parked` event.

```sh
quietroom-node --config /etc/quietroom/config.toml --outbox list --channel fleet --parked
quietroom-node --config /etc/quietroom/config.toml --outbox requeue --channel fleet --all
```

## Stopping the daemon

`SIGTERM` (`systemctl stop`) and `SIGINT` stop the node cleanly. The current
sweep finishes the segment in progress; segments not yet started are abandoned
(they run on the next start). The node sends `STOPPING=1` to systemd, makes one
last flush of the alert and fleet outboxes (anything undelivered stays queued on
disk), closes the outboxes, store and SDR, and exits 0. A clean stop leaves no
`node.db-wal`/`-shm` behind. A flush to an unreachable sink can cost one network
timeout per sink, so the unit sets `TimeoutStopSec=60`. A second signal during
shutdown kills the process with the default action.

## SQLite databases

The store, the alert outbox and the fleet outbox all open through
`quietroom/sqlitedb.py` (`open_db` + a declarative `Schema`). It sets a busy
timeout, switches the store to WAL with a retry on lock contention, and creates
and upgrades the schema inside `BEGIN IMMEDIATE`, so two processes opening an old
file at once (the daemon and `--outbox`/`--check-config`) serialise instead of
racing; a column another process already added is treated as done. A test
(`tests/test_sqlite_discipline.py`) fails if any other module runs DDL
(`ALTER/CREATE/DROP`, `PRAGMA journal_mode`), `executescript` or
`sqlite3.connect`.
