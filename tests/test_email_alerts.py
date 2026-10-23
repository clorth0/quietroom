"""Email alerts wired into quietroom-node: SMTP config, env-only secrets, outbox retries."""

import json
import sqlite3
import ssl
from email import message_from_string

import pytest

from quietroom.alerts import EmailNotifier, Outbox, QueuedChannel
from quietroom.config import EmailConfig, NodeConfig
from quietroom.node.cli import build_alerting, main

from _fake_smtp import FakeSMTP, make_ca_and_server_cert

pytest.importorskip("cryptography")

SEG = [{"name": "s", "start_hz": 300_000_000, "stop_hz": 301_000_000}]


def node_cfg(email=None, **alerts):
    if email is not None:
        alerts["email"] = email
    return NodeConfig.from_dict({"node": {"id": "n1"}, "alerts": alerts, "segments": SEG})


@pytest.fixture
def tls(tmp_path):
    return make_ca_and_server_cert(tmp_path)


@pytest.fixture
def server():
    servers = []

    def make(**kw):
        s = FakeSMTP(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


ALERT = {"node_id": "n1", "segment": "uhf", "freq_hz": 308e6, "score": 91.2, "reasons": ["x"]}


# --- config ----------------------------------------------------------------


def test_email_section_parses_with_defaults():
    c = node_cfg({"host": "smtp.example.test", "from": "qr@example.test", "to": "ops@example.test"})
    e = c.email
    assert isinstance(e, EmailConfig)
    assert (e.host, e.port, e.starttls, e.ssl) == ("smtp.example.test", 587, True, False)
    assert e.to_addrs == ("ops@example.test",) and e.username == ""


def test_email_section_list_recipients_and_implicit_tls():
    c = node_cfg(
        {"host": "h", "port": 465, "ssl": True, "starttls": False, "from": "a@x.test",
         "to": ["b@x.test", "c@x.test"], "username": "qr"}
    )
    assert c.email.to_addrs == ("b@x.test", "c@x.test") and c.email.ssl


def test_no_email_section_means_no_email():
    assert node_cfg().email is None


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"from": "a@x.test", "to": "b@x.test"}, "host"),
        ({"host": "h", "to": "b@x.test"}, "from"),
        ({"host": "h", "from": "a@x.test"}, "to"),
        ({"host": "h", "from": "a@x.test", "to": []}, "to"),
        ({"host": "h", "from": "not-an-address", "to": "b@x.test"}, "address"),
        ({"host": "h", "from": "a@x.test", "to": "b@x.test\r\nBcc: evil@x.test"}, "address"),
        ({"host": "h", "from": "a@x.test", "to": "b@x.test", "port": 0}, "port"),
        ({"host": "h", "from": "a@x.test", "to": "b@x.test", "ssl": True, "starttls": True},
         "both"),
        (
            {"host": "h", "from": "a@x.test", "to": "b@x.test", "starttls": False,
             "username": "u"},
            "plaintext",
        ),
        ({"host": "h", "from": "a@x.test", "to": "b@x.test", "password": "hunter2"},
         "QUIETROOM_SMTP_PASSWORD"),
        ({"host": "h", "from": "a@x.test", "to": "b@x.test", "tls": True}, "unknown"),
    ],
)
def test_email_section_rejects(bad, msg):
    with pytest.raises(ValueError, match=msg):
        node_cfg(bad)


# --- notifier against a real socket ----------------------------------------


def test_starttls_auth_and_message_content(server, tls):
    ca, ctx = tls
    srv = server(tls_context=ctx)
    n = EmailNotifier(
        "qr@x.test", ["ops@x.test"], host="localhost", port=srv.port, starttls=True,
        username="qr", password="s3cret", ca_file=str(ca), timeout=5,
    )
    n.send(ALERT)
    assert srv.auth_attempts == [("qr", "s3cret", True)]  # creds only ever sent inside TLS
    (m,) = srv.messages
    assert m.tls and m.rcpt_to == ["ops@x.test"] and m.mail_from == "qr@x.test"
    msg = message_from_string(m.data)
    assert "n1" in msg["Subject"] and "308.000 MHz" in msg["Subject"]
    assert json.loads(msg.get_payload())["score"] == 91.2


def test_unverified_server_certificate_is_refused(server, tls):
    _, ctx = tls
    srv = server(tls_context=ctx)
    n = EmailNotifier("qr@x.test", ["ops@x.test"], host="localhost", port=srv.port,
                      starttls=True, username="qr", password="s3cret", timeout=5)
    with pytest.raises(ssl.SSLError):
        n.send(ALERT)
    assert srv.auth_attempts == [] and srv.messages == []


def test_server_without_starttls_is_refused_when_starttls_required(server):
    srv = server()  # offers no STARTTLS
    n = EmailNotifier("qr@x.test", ["ops@x.test"], host="localhost", port=srv.port,
                      starttls=True, username="qr", password="s3cret", timeout=5)
    import smtplib

    with pytest.raises(smtplib.SMTPNotSupportedError, match="STARTTLS"):
        n.send(ALERT)
    assert srv.auth_attempts == [] and srv.messages == []


def test_implicit_tls(server, tls):
    ca, ctx = tls
    srv = server(tls_context=ctx, implicit_tls=True)
    n = EmailNotifier("qr@x.test", ["ops@x.test"], host="localhost", port=srv.port,
                      starttls=False, ssl=True, ca_file=str(ca), timeout=5)
    n.send(ALERT)
    assert srv.messages and srv.messages[0].tls


def test_local_relay_without_auth(server):
    srv = server()
    EmailNotifier("qr@x.test", ["ops@x.test"], host="127.0.0.1", port=srv.port,
                  starttls=False, timeout=5).send(ALERT)
    assert srv.messages and srv.messages[0].auth is None


# --- outbox ----------------------------------------------------------------


def test_outbox_channels_are_independent(tmp_path):
    a = Outbox(tmp_path / "o.db", channel="email")
    b = Outbox(tmp_path / "o.db", channel="webhook")
    a.add({"x": 1})
    assert (a.pending(), b.pending()) == (1, 0)
    assert b.flush(lambda p: None) == 0 and a.pending() == 1


def test_outbox_backoff_and_stop_on_failure(tmp_path):
    clock = [100.0]
    o = Outbox(tmp_path / "o.db", channel="email", backoff_s=30, max_backoff_s=100,
               clock=lambda: clock[0])
    o.add({"n": 1})
    o.add({"n": 2})
    calls = []

    def down(p):
        calls.append(p["n"])
        raise OSError("connection refused")

    assert o.flush(down) == 0
    assert calls == [1]  # server is down: don't burn a timeout per queued alert
    assert o.flush(down) == 0 and calls == [1]  # still backing off
    clock[0] = 131.0
    o.flush(down)
    assert calls == [1, 1]
    row = o.db.execute("SELECT attempts,next_attempt_at,last_error FROM outbox "
                       "WHERE channel='email' ORDER BY id").fetchone()
    assert row[0] == 2 and row[1] == 131.0 + 60 and "refused" in row[2]
    clock[0] = 10_000.0
    sent = []
    assert o.flush(sent.append) == 2 and [p["n"] for p in sent] == [1, 2]


def test_outbox_migrates_pre_channel_schema(tmp_path):
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY, payload TEXT, attempts INTEGER "
               "DEFAULT 0)")
    db.execute("INSERT INTO outbox(payload) VALUES('{\"legacy\": 1}')")
    db.commit()
    db.close()
    o = Outbox(p)  # default channel picks up legacy rows
    got = []
    assert o.flush(got.append) == 1 and got == [{"legacy": 1}]


def test_queued_channel_enqueues_then_flushes(tmp_path):
    sent = []
    ch = QueuedChannel("email", sent.append, Outbox(tmp_path / "o.db", channel="email"))
    ch(ALERT)
    assert sent == [] and ch.pending() == 1
    assert ch.flush() == 1 and sent[0]["freq_hz"] == 308e6


# --- CLI wiring ------------------------------------------------------------


def test_build_alerting_reads_password_from_env_only(tmp_path):
    cfg = node_cfg({"host": "h", "from": "a@x.test", "to": "b@x.test", "username": "qr"})
    engine, channels = build_alerting(cfg, {"QUIETROOM_SMTP_PASSWORD": "pw"})
    (email,) = channels
    assert engine is not None and email.name == "email"
    assert email.notifier.password == "pw"
    with pytest.raises(SystemExit, match="QUIETROOM_SMTP_PASSWORD"):
        build_alerting(cfg, {})


def test_build_alerting_webhook_and_email_each_get_a_queue(tmp_path):
    cfg = NodeConfig.from_dict({
        "node": {"id": "n1"},
        "store": {"path": str(tmp_path / "q.db")},
        "alerts": {"webhook_url": "https://hook.test/x",
                   "email": {"host": "127.0.0.1", "starttls": False, "from": "a@x.test",
                             "to": "b@x.test"}},
        "segments": SEG,
    })
    engine, channels = build_alerting(cfg, {})
    assert [c.name for c in channels] == ["webhook", "email"]
    assert all(isinstance(c, QueuedChannel) for c in channels)


def test_no_alert_sinks_means_no_engine():
    assert build_alerting(node_cfg(), {}) == (None, [])


def _write_cfg(tmp_path, port, extra_email=""):
    p = tmp_path / "node.toml"
    p.write_text(f"""
[node]
id = "floor3-east"
baseline_cycles = 3
[store]
path = "{tmp_path / 'q.db'}"
[alerts]
min_score = 60
[alerts.email]
host = "127.0.0.1"
port = {port}
starttls = false
from = "quietroom@example.test"
to = ["secops@example.test"]
{extra_email}
[[segments]]
name = "covert-uhf"
start = "300M"
stop = "320M"
bin = "25k"
""")
    return p


def test_node_once_emails_the_demo_bug(tmp_path, server, capsys):
    srv = server()
    rc = main(["--config", str(_write_cfg(tmp_path, srv.port)), "--demo", "--once"])
    assert rc == 0
    (m,) = srv.messages
    msg = message_from_string(m.data)
    assert msg["To"] == "secops@example.test" and "308.000 MHz" in msg["Subject"]
    health = json.loads(capsys.readouterr().out.splitlines()[-1])["health"]
    assert health["alert_outbox"] == {"email": 0}


def test_node_once_queues_when_smtp_down_and_delivers_next_run(tmp_path, server, capsys):
    srv = server(fail_next=1)
    cfg = _write_cfg(tmp_path, srv.port)
    main(["--config", str(cfg), "--demo", "--once"])
    health = json.loads(capsys.readouterr().out.splitlines()[-1])["health"]
    assert srv.messages == [] and health["alert_outbox"] == {"email": 1}
    assert health["last_error"] is None  # delivery trouble is not a sweep error
    # Next process start (systemd restart, or the next --once) retries the queue.
    # The first backoff is 30 s; a fresh process honours next_attempt_at too, so
    # move it into the past as the clock would.
    db = sqlite3.connect(str(tmp_path / "q.db") + ".alerts-outbox")
    db.execute("UPDATE outbox SET next_attempt_at=0")
    db.commit()
    main(["--config", str(cfg), "--demo", "--once"])
    assert len(srv.messages) >= 1
    assert "308.000 MHz" in message_from_string(srv.messages[0].data)["Subject"]
