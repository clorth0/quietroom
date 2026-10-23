"""quietroom-node: headless daemon entry point."""

from __future__ import annotations
import argparse
import json
import os
import ssl
import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from quietroom.config import FleetConfig, NodeConfig, default_config, load_config, parse_freq
from quietroom.node.service import MonitorService

if TYPE_CHECKING:
    from quietroom.fleet import FleetClient

CENTRAL_TOKEN_ENV = "QUIETROOM_CENTRAL_TOKEN"


def _fleet_outbox(cfg: NodeConfig) -> str:
    return (cfg.store_path + ".fleet-outbox") if cfg.store_path else ":memory:"


def build_fleet(cfg: NodeConfig, env: Mapping[str, str]) -> tuple[FleetClient, list[str]]:
    """FleetClient for ``cfg.fleet`` plus hardening warnings; SystemExit on misconfiguration.

    mTLS: the node UUID (``identity_path``) must equal the UUID in the client
    certificate, and the legacy token is never sent even if it is in the env.
    """
    from quietroom.fleet import (
        FleetClient,
        NodeIdentity,
        cert_file_identity,
        client_tls_context,
    )
    from quietroom.update import check_secret_file

    fc: FleetConfig | None = cfg.fleet
    if fc is None:
        raise ValueError("no [fleet] base_url configured")
    if fc.auth == "legacy-token":
        token = env.get(CENTRAL_TOKEN_ENV, "")
        if not token:
            raise SystemExit(f'[fleet] auth = "legacy-token" needs ${CENTRAL_TOKEN_ENV}')
        pinned = ssl.create_default_context(cafile=fc.ca_file) if fc.ca_file else None
        client = FleetClient(
            fc.base_url, token=token, outbox_path=_fleet_outbox(cfg), ssl_context=pinned
        )
        return client, [
            "fleet auth is the legacy shared bearer token (deprecated): any holder can "
            "impersonate any node; migrate to per-node mTLS"
        ]
    node_uuid = NodeIdentity.load_or_create(fc.identity_path).node_id
    try:
        ctx = client_tls_context(fc.cert_file, fc.key_file, fc.ca_file)
    except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
        raise SystemExit(f"[fleet] cannot load mTLS material: {e}") from e
    warnings = check_secret_file(fc.key_file)
    try:
        cert_uuid = cert_file_identity(fc.cert_file)
    except NotImplementedError:
        warnings.append("cannot self-check the client certificate UUID on this Python")
    else:
        if cert_uuid is None:
            raise SystemExit(
                f"[fleet] {fc.cert_file} carries no node UUID (SAN urn:uuid:<uuid> or UUID CN)"
            )
        if cert_uuid != node_uuid:
            raise SystemExit(
                f"[fleet] client certificate is for node {cert_uuid} but this node is "
                f"{node_uuid} ({fc.identity_path}); issue a certificate for {node_uuid}"
            )
    return (
        FleetClient(
            fc.base_url, outbox_path=_fleet_outbox(cfg), ssl_context=ctx, node_uuid=node_uuid
        ),
        warnings,
    )


def _device(demo, cfg=None):
    if demo:
        from quietroom.radio.recorded import streaming_demo_device

        # Keep every baseline capture clean: the default 3 clean sweeps was
        # shorter than baseline_cycles (5), so the demo learned its own bug.
        n = cfg.baseline_cycles * sum(s.enabled for s in cfg.segments) if cfg else 3
        return streaming_demo_device(n_clean=n)
    from quietroom.radio.hackrf import HackRFDevice

    return HackRFDevice()


SMTP_PASSWORD_ENV = "QUIETROOM_SMTP_PASSWORD"
WEBHOOK_SECRET_ENV = "QUIETROOM_WEBHOOK_SECRET"


def build_alerting(cfg, env):
    """Alert engine plus one queued delivery channel per configured sink.

    Every channel goes through a SQLite Outbox (next to the store when one is
    configured, else in memory), so a failed send is retried with backoff on
    later ticks instead of being lost or raised into the sweep loop. Secrets
    come from `env` only.
    """
    from quietroom.alerts import (
        AlertEngine,
        AlertRule,
        EmailNotifier,
        Outbox,
        QueuedChannel,
        WebhookNotifier,
    )

    sinks = []
    if cfg.webhook_url:
        sinks.append(
            ("webhook", WebhookNotifier(cfg.webhook_url, secret=env.get(WEBHOOK_SECRET_ENV)).send)
        )
    if cfg.email is not None:
        e = cfg.email
        password = env.get(SMTP_PASSWORD_ENV)
        if e.username and not password:
            raise SystemExit(
                f"[alerts.email] username {e.username!r} set but {SMTP_PASSWORD_ENV} is empty"
            )
        sinks.append(
            (
                "email",
                EmailNotifier(
                    e.from_addr,
                    e.to_addrs,
                    host=e.host,
                    port=e.port,
                    starttls=e.starttls,
                    ssl=e.ssl,
                    username=e.username,
                    password=password,
                    ca_file=e.ca_file,
                    timeout=e.timeout_s,
                ).send,
            )
        )
    if not sinks:
        return None, []
    path = _alerts_outbox(cfg) if cfg.store_path else ":memory:"
    channels = [
        QueuedChannel(
            name,
            send,
            Outbox(
                path, channel=name, backoff_s=30, max_backoff_s=3600,
                max_attempts=cfg.alert_max_attempts, max_age_s=cfg.alert_max_age_s,
            ),
        )
        for name, send in sinks
    ]
    rule = AlertRule(min_score=cfg.alert_min_score, cooldown_s=cfg.alert_cooldown_s)
    engine = AlertEngine([rule])
    return engine, channels


FORCE_WARNING = (
    "WARNING: --force re-learns this segment even while an incident is active.\n"
    "  Whatever is transmitting right now - including a planted device - becomes part\n"
    "  of the baseline and stops being reported by live sweeps. It stays flagged only\n"
    "  as divergence from the anchor baseline, and automatic refreshes stay held,\n"
    "  until you accept it with --promote-anchor. The override is recorded in the\n"
    "  baseline's version history and sent to central as a 'control' event."
)

_WAIT_EXIT = {"done": 0, "failed": 1, "refused": 2}


def queue_control(cfg: NodeConfig, a: argparse.Namespace) -> int:
    """Queue an operator command for the running node; optionally wait for it."""
    command, segment = next(
        (c, getattr(a, attr))
        for c, attr in (
            ("rebaseline", "rebaseline"), ("ack", "ack"), ("promote-anchor", "promote_anchor")
        )
        if getattr(a, attr)
    )
    if not cfg.store_path:
        raise SystemExit(
            f"--{command} needs a [store] path: the running node picks requests up from its store"
        )
    seg = next((s for s in cfg.segments if s.name == segment), None)
    if seg is None:
        names = ", ".join(s.name for s in cfg.segments)
        raise SystemExit(f"unknown segment {segment!r} (configured: {names})")
    if not seg.enabled:
        raise SystemExit(f"segment {segment!r} is disabled in the config")
    args: dict[str, object] = {"note": a.note or ""}
    if command == "rebaseline":
        args["force"] = bool(a.force)
    if command == "ack" and a.freq is not None:
        try:
            args["freq_hz"] = parse_freq(a.freq)
        except ValueError as e:
            raise SystemExit(f"--freq: {e}") from e
    if a.force:
        print(FORCE_WARNING, file=sys.stderr)
    from quietroom.store import Store

    store = Store(cfg.store_path)
    rid = store.request_control(cfg.node_id, segment, command, args)
    print(json.dumps(store.control_request(rid)), flush=True)
    if not a.wait:
        return 0
    deadline = time.monotonic() + a.wait
    while time.monotonic() < deadline:
        req = store.control_request(rid)
        if req is not None and req["status"] != "pending":
            print(json.dumps(req))
            return _WAIT_EXIT.get(req["status"], 1)
        time.sleep(0.1)
    print(
        f"request {rid} still pending after {a.wait:g}s: is quietroom-node running "
        f"with store {cfg.store_path}? It stays queued and is applied on the node's next tick.",
        file=sys.stderr,
    )
    return 3
def _alerts_outbox(cfg: NodeConfig) -> str:
    return cfg.store_path + ".alerts-outbox"


def outbox_command(cfg: NodeConfig, a: argparse.Namespace) -> int:
    """``--outbox list|requeue``: inspect / requeue the alert outbox file directly.

    Safe while the node runs (SQLite locking); a requeued alert goes out on the
    node's next tick.
    """
    from quietroom.alerts import outbox_rows, requeue_parked

    if not cfg.store_path:
        raise SystemExit("--outbox needs a [store] path (the outbox lives next to the store)")
    path = _alerts_outbox(cfg)
    if a.outbox == "list":
        if not os.path.exists(path):
            return 0
        for r in outbox_rows(path, channel=a.channel, parked=True if a.parked else None):
            r["state"] = "parked" if r["parked_at"] is not None else "pending"
            print(json.dumps(r, default=str))
        return 0
    if not os.path.exists(path):
        print("nothing parked: no alert outbox yet", file=sys.stderr)
        return 1
    ids = None if a.all else a.ids
    got = requeue_parked(path, ids=ids, channel=a.channel)
    print(json.dumps({"requeued": got}))
    if ids is not None and set(ids) - set(got):
        missing = sorted(set(ids) - set(got))
        print(f"not parked (or no such row): {missing}", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="quietroom-node", description="Quietroom unattended monitoring node"
    )
    p.add_argument("--config", help="TOML node config (see config.example.toml)")
    p.add_argument("--demo", action="store_true", help="run with demo device")
    p.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config and that every env secret / mTLS file it needs is present "
        "(values are never printed); exit 1 on errors",
    )
    p.add_argument(
        "--print-identity",
        action="store_true",
        help="print this node's fleet UUID (creating it if needed) for certificate issuance",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="capture baselines, run one tick, print events as JSON lines",
    )
    ctl = p.add_argument_group(
        "operator commands",
        "Queued in the node's store and applied by the running quietroom-node on its next "
        "tick (it owns the radio). Run as the service user.",
    )
    one = ctl.add_mutually_exclusive_group()
    one.add_argument(
        "--rebaseline", metavar="SEGMENT",
        help="re-learn SEGMENT's baseline now; refused while an incident holds learning",
    )
    one.add_argument(
        "--ack", metavar="SEGMENT",
        help="incident resolved: release SEGMENT's learning hold early (one emitter with "
        "--freq, else all); an emitter still on air re-freezes it on the next sweep",
    )
    one.add_argument(
        "--promote-anchor", metavar="SEGMENT",
        help="accept SEGMENT's current baseline as its long-term anchor (reference)",
    )
    ctl.add_argument(
        "--force", action="store_true",
        help="with --rebaseline: override an active incident (learns it; see warning)",
    )
    ctl.add_argument("--freq", help="with --ack: the emitter frequency, e.g. 308M")
    ctl.add_argument("--note", help="free text recorded with the request (audit trail)")
    ctl.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="wait for the node to apply it; exit 0 done, 1 failed, 2 refused, 3 timeout",
    )
    ob = p.add_argument_group("alert outbox (dead letters)")
    ob.add_argument(
        "--outbox", choices=("list", "requeue"),
        help="list queued/parked alerts as JSON lines, or requeue parked ones",
    )
    ob.add_argument("--channel", help="with --outbox: only this channel (email, webhook)")
    ob.add_argument("--parked", action="store_true", help="with --outbox list: parked only")
    ob.add_argument(
        "--id", dest="ids", type=int, action="append",
        help="with --outbox requeue: row id (repeatable)",
    )
    ob.add_argument("--all", action="store_true", help="with --outbox requeue: every parked row")
    a = p.parse_args(argv)
    if a.force and not a.rebaseline:
        p.error("--force only applies to --rebaseline")
    if a.freq is not None and not a.ack:
        p.error("--freq only applies to --ack")
    if a.outbox != "requeue" and (a.ids or a.all):
        p.error("--id/--all only apply to --outbox requeue")
    if a.outbox == "requeue" and bool(a.ids) == bool(a.all):
        p.error("--outbox requeue needs exactly one of --id N or --all")
    if a.outbox != "list" and a.parked:
        p.error("--parked only applies to --outbox list")
    if a.outbox is None and a.channel:
        p.error("--channel only applies to --outbox")
    cfg = load_config(a.config) if a.config else default_config()
    if a.outbox:
        return outbox_command(cfg, a)
    if a.check_config:
        from quietroom.node.checkconfig import check_node_config

        report = check_node_config(cfg, os.environ)
        print(json.dumps({"config": cfg.to_dict(), **report}, indent=2))
        for e in report["errors"]:
            print(f"ERROR: {e}", file=sys.stderr)
        for w in report["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        return 0 if report["ok"] else 1
    if a.print_identity:
        from quietroom.fleet import NodeIdentity

        if cfg.fleet is None or not cfg.fleet.identity_path:
            raise SystemExit("--print-identity needs [fleet] identity_path (or a [store] path)")
        print(NodeIdentity.load_or_create(cfg.fleet.identity_path).node_id)
        return 0
    if a.rebaseline or a.ack or a.promote_anchor:
        return queue_control(cfg, a)
    events = []
    store = None
    if cfg.store_path:
        from quietroom.store import Store

        store = Store(cfg.store_path)
        store.upsert_node(cfg.node_id, cfg.site, cfg.floor, cfg.room)
    alert_engine, notifiers = build_alerting(cfg, os.environ)
    fleet = None
    fleet_warnings: list[str] = []
    if cfg.fleet is not None:
        fleet, fleet_warnings = build_fleet(cfg, os.environ)
    from quietroom.node.watchdog import SdNotifier

    svc = MonitorService(
        cfg,
        _device(a.demo, cfg),
        # Only --once prints events; a daemon keeping every event would grow forever.
        sink=events.append if a.once else None,
        store=store,
        alert_engine=alert_engine,
        notifiers=notifiers,
        fleet=fleet,
        fleet_interval_s=cfg.fleet_heartbeat_s,
        fleet_warnings=fleet_warnings,
        notifier=SdNotifier(),
    )
    if a.once:
        svc.start()
        r = svc.tick()
        for e in events:
            print(json.dumps(e))
        print(json.dumps({"health": svc.health(), "tick": r.__dict__}, default=str))
        return 0
    print(f"quietroom-node {cfg.node_id} starting ({len(cfg.segments)} segments)")
    svc.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
