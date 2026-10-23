"""quietroom-node: headless daemon entry point."""

from __future__ import annotations
import argparse
import json
import os
from quietroom.config import default_config, load_config
from quietroom.node.service import MonitorService


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
        "--once",
        action="store_true",
        help="capture baselines, run one tick, print events as JSON lines",
    )
    a = p.parse_args(argv)
    cfg = load_config(a.config) if a.config else default_config()
    if a.check_config:
        print(json.dumps(cfg.to_dict(), indent=2))
        return 0
    events = []
    store = None
    if cfg.store_path:
        from quietroom.store import Store

        store = Store(cfg.store_path)
        store.upsert_node(cfg.node_id, cfg.site, cfg.floor, cfg.room)
    alert_engine, notifiers = build_alerting(cfg, os.environ)
    fleet = None
    if cfg.fleet_base_url:
        from quietroom.fleet import FleetClient

        fleet = FleetClient(
            cfg.fleet_base_url,
            token=os.environ.get("QUIETROOM_CENTRAL_TOKEN"),
            outbox_path=(cfg.store_path + ".fleet-outbox") if cfg.store_path else ":memory:",
        )
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
