"""quietroom-node: headless daemon entry point."""

from __future__ import annotations
import argparse
import json
import os
import ssl
from collections.abc import Mapping
from typing import TYPE_CHECKING

from quietroom.config import FleetConfig, NodeConfig, default_config, load_config
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
    path = (cfg.store_path + ".alerts-outbox") if cfg.store_path else ":memory:"
    channels = [
        QueuedChannel(name, send, Outbox(path, channel=name, backoff_s=30, max_backoff_s=3600))
        for name, send in sinks
    ]
    rule = AlertRule(min_score=cfg.alert_min_score, cooldown_s=cfg.alert_cooldown_s)
    engine = AlertEngine([rule])
    return engine, channels


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="quietroom-node", description="Quietroom unattended monitoring node"
    )
    p.add_argument("--config", help="TOML node config (see config.example.toml)")
    p.add_argument("--demo", action="store_true", help="run with demo device")
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
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
    a = p.parse_args(argv)
    cfg = load_config(a.config) if a.config else default_config()
    if a.check_config:
        print(json.dumps(cfg.to_dict(), indent=2))
        return 0
    if a.print_identity:
        from quietroom.fleet import NodeIdentity

        if cfg.fleet is None or not cfg.fleet.identity_path:
            raise SystemExit("--print-identity needs [fleet] identity_path (or a [store] path)")
        print(NodeIdentity.load_or_create(cfg.fleet.identity_path).node_id)
        return 0
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
        sink=events.append,
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
