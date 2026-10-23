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

## Alert secrets

SMTP and webhook secrets are read from the environment only
(`QUIETROOM_SMTP_PASSWORD`, `QUIETROOM_WEBHOOK_SECRET`); the config loader
rejects a `password` key in `[alerts.email]`. Under systemd put them in
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
[--allow-legacy-token] --check-config` does the same for
`QUIETROOM_CENTRAL_TOKEN`, `QUIETROOM_CENTRAL_READ_TOKEN` and the TLS files.

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
