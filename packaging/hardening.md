# Deployment hardening checklist (nodes)

Implemented in code/packaging: systemd sandboxing + watchdog (packaging/systemd),
group-scoped SDR USB access (packaging/udev), HMAC-signed staged updates with
rollback (quietroom/update.py), secret-file permission checks
(`check_secret_file`).

Operator/site responsibilities until automated:
- Full-disk or data-partition encryption for /var/lib/quietroom (SQLite, keys).
- Per-node credentials: unique update key and central token per node; rotate on
  staff changes; revoke at the central server — shared fleet keys are a stopgap.
- Local web UI stays loopback-only, or behind TLS + individual accounts; never
  expose Werkzeug directly (see SECURITY.md).
- Read-only root filesystem where the OS supports it, with /var/lib/quietroom
  on a separate writable partition; disable password SSH; outbound-only egress
  to the central server and update host.
- Physical: locked/enclosed mounting; a missed heartbeat is treated as a
  node-down/security event, not proof of tampering.
- Updates: stage during a maintenance window, verify `current_version()`,
  keep one known-good `previous` release; test rollback quarterly.
