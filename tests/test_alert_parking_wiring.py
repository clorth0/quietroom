"""Dead-letter wiring: config, service health/events, heartbeat, quietroom-node --outbox."""

import json

import pytest

from quietroom.alerts import Outbox, QueuedChannel, outbox_rows
from quietroom.config import NodeConfig
from quietroom.fleet import NodeIdentity, heartbeat_payload
from quietroom.node.cli import build_alerting, main
from quietroom.node.service import MonitorService

SEG = [{"name": "s", "start_hz": 300_000_000, "stop_hz": 301_000_000}]
HOOK = "https://hook.test/x"


def cfg(tmp_path=None, **alerts):
    d = {"node": {"id": "n1"}, "alerts": {"webhook_url": HOOK, **alerts}, "segments": SEG}
    if tmp_path is not None:
        d["store"] = {"path": str(tmp_path / "q.db")}
    return NodeConfig.from_dict(d)


def test_limits_default_and_parse():
    c = cfg()
    assert (c.alert_max_attempts, c.alert_max_age_s) == (5, 7 * 86400.0)
    c = cfg(max_attempts=0, max_age_s=0)
    assert (c.alert_max_attempts, c.alert_max_age_s) == (0, 0.0)


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"max_attempts": -1}, "max_attempts"),
        ({"max_attempts": 2.5}, "max_attempts"),
        ({"max_attempts": True}, "max_attempts"),
        ({"max_age_s": -5}, "max_age_s"),
        ({"max_age_s": "week"}, "max_age_s"),
    ],
)
def test_limits_are_validated(bad, msg):
    with pytest.raises(ValueError, match=msg):
        cfg(**bad)


def test_build_alerting_applies_the_limits_to_every_channel(tmp_path):
    _, (hook,) = build_alerting(cfg(tmp_path, max_attempts=7, max_age_s=60), {})
    assert (hook.outbox.max_attempts, hook.outbox.max_age_s) == (7, 60.0)


def _svc(tmp_path, events, clock):
    o = Outbox(tmp_path / "o.db", channel="email", clock=clock, max_age_s=10)
    ch = QueuedChannel("email", lambda p: (_ for _ in ()).throw(OSError("down")), o)
    return MonitorService(cfg(), None, sink=events.append, notifiers=[ch]), ch


def test_parked_alerts_show_in_health_and_raise_an_event(tmp_path):
    t = [1000.0]
    events = []
    svc, ch = _svc(tmp_path, events, lambda: t[0])
    ch({"segment": "uhf", "freq_hz": 308e6, "score": 90.0, "node_id": "n1"})
    svc.flush_alerts()
    assert svc.health()["alert_parked"] == {"email": 0}
    t[0] += 11
    svc.flush_alerts()
    h = svc.health()
    assert h["alert_parked"] == {"email": 1} and h["alert_outbox"] == {"email": 0}
    [e] = [e for e in events if e["type"] == "alert_parked"]
    assert e["channel"] == "email" and e["node_id"] == "n1"
    assert e["parked"][0]["reason"].startswith("max_age") and "payload" not in e["parked"][0]
    assert "--outbox" in e["hint"]


def test_heartbeat_carries_outbox_and_parked_counts():
    hb = heartbeat_payload(
        NodeIdentity("n1", "hq", "3", "lab"),
        {"alert_outbox": {"email": 2}, "alert_parked": {"email": 1},
         "alert_delivery_errors": {"email": "OSError: x"}},
        1.0,
    )
    assert hb["alert_outbox"] == {"email": 2} and hb["alert_parked"] == {"email": 1}
    assert hb["alert_delivery_errors"] == {"email": "OSError: x"}


# --- quietroom-node --outbox ------------------------------------------------


@pytest.fixture
def parked(tmp_path):
    """A node config whose outbox holds one parked email and one pending webhook alert."""
    p = tmp_path / "node.toml"
    p.write_text(
        f'[node]\nid = "n1"\n[store]\npath = "{tmp_path / "q.db"}"\n'
        '[[segments]]\nname = "s"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n'
    )
    path = str(tmp_path / "q.db") + ".alerts-outbox"
    t = [1000.0]
    email = Outbox(path, channel="email", clock=lambda: t[0], max_age_s=10)
    hook = Outbox(path, channel="webhook", clock=lambda: t[0])
    email.add({"node_id": "n1", "segment": "s", "freq_hz": 308e6, "score": 88})
    hook.add({"node_id": "n1", "segment": "s", "freq_hz": 309e6, "score": 81})
    t[0] += 20
    email.flush(lambda p: (_ for _ in ()).throw(OSError("535 auth failed")))
    return p, path


def lines(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]


def test_outbox_list_shows_rows_with_state(parked, capsys):
    conf, _ = parked
    assert main(["--config", str(conf), "--outbox", "list"]) == 0
    rows = lines(capsys)
    assert [(r["id"], r["channel"], r["state"]) for r in rows] == [
        (1, "email", "parked"), (2, "webhook", "pending")
    ]
    assert rows[0]["summary"] == "n1 s 308.000 MHz score 88"
    assert main(["--config", str(conf), "--outbox", "list", "--parked"]) == 0
    assert [r["id"] for r in lines(capsys)] == [1]
    assert main(["--config", str(conf), "--outbox", "list", "--channel", "webhook"]) == 0
    assert [r["id"] for r in lines(capsys)] == [2]


def test_outbox_requeue_by_id(parked, capsys):
    conf, path = parked
    assert main(["--config", str(conf), "--outbox", "requeue", "--id", "1"]) == 0
    assert lines(capsys) == [{"requeued": [1]}]
    [row] = outbox_rows(path, channel="email")
    assert row["parked_at"] is None and row["attempts"] == 0


def test_outbox_requeue_all_and_nothing_to_do(parked, capsys):
    conf, _ = parked
    assert main(["--config", str(conf), "--outbox", "requeue", "--all"]) == 0
    assert lines(capsys) == [{"requeued": [1]}]
    assert main(["--config", str(conf), "--outbox", "requeue", "--id", "1"]) == 1
    assert "not parked" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--outbox", "requeue"],  # needs --id or --all
        ["--outbox", "requeue", "--id", "1", "--all"],
        ["--outbox", "list", "--id", "1"],
        ["--outbox", "list", "--all"],
        ["--parked"],
        ["--outbox", "drop"],
    ],
)
def test_outbox_argument_errors(parked, argv):
    conf, _ = parked
    with pytest.raises(SystemExit):
        main(["--config", str(conf), *argv])


def test_outbox_needs_a_store(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text('[[segments]]\nname = "s"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n')
    with pytest.raises(SystemExit, match=r"\[store\] path"):
        main(["--config", str(p), "--outbox", "list"])


def test_outbox_list_does_not_create_a_missing_outbox(tmp_path, capsys):
    p = tmp_path / "n.toml"
    p.write_text(
        f'[store]\npath = "{tmp_path / "q.db"}"\n'
        '[[segments]]\nname = "s"\nstart_hz = 300_000_000\nstop_hz = 301_000_000\n'
    )
    assert main(["--config", str(p), "--outbox", "list"]) == 0
    assert lines(capsys) == [] and not (tmp_path / "q.db.alerts-outbox").exists()
