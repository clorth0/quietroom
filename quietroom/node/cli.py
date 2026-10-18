"""quietroom-node: headless daemon entry point."""

from __future__ import annotations
import argparse
import json
from quietroom.config import default_config, load_config
from quietroom.node.service import MonitorService


def _device(demo):
    if demo:
        from quietroom.radio.recorded import streaming_demo_device

        return streaming_demo_device()
    from quietroom.radio.hackrf import HackRFDevice

    return HackRFDevice()


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
    alert_engine = None
    notifiers = []
    if cfg.webhook_url:
        import os
        from quietroom.alerts import AlertEngine, AlertRule, WebhookNotifier

        alert_engine = AlertEngine(
            [AlertRule(min_score=cfg.alert_min_score, cooldown_s=cfg.alert_cooldown_s)]
        )
        notifiers = [
            WebhookNotifier(cfg.webhook_url, secret=os.environ.get("QUIETROOM_WEBHOOK_SECRET")).send
        ]
    fleet = None
    if cfg.fleet_base_url:
        import os
        from quietroom.fleet import FleetClient

        fleet = FleetClient(
            cfg.fleet_base_url,
            token=os.environ.get("QUIETROOM_CENTRAL_TOKEN"),
            outbox_path=(cfg.store_path + ".fleet-outbox") if cfg.store_path else ":memory:",
        )
    from quietroom.node.watchdog import SdNotifier

    svc = MonitorService(
        cfg,
        _device(a.demo),
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
