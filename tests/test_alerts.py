import hashlib
import hmac
import json
from quietroom.alerts import AlertEngine, AlertRule, WebhookNotifier, EmailNotifier, Outbox


def ev(score=90.0, freq=308e6, t=100.0):
    return {
        "node_id": "n1",
        "segment": "uhf",
        "timestamp": t,
        "findings": [
            {
                "freq_hz": freq,
                "score": score,
                "raw_score": 100.0,
                "band": "unknown",
                "reasons": ["x"],
            }
        ],
    }


def test_engine_threshold_dedup_cooldown():
    e = AlertEngine([AlertRule(min_score=80, cooldown_s=60)])
    assert len(e.process(ev(t=100.0))) == 1
    assert e.process(ev(t=110.0)) == []  # cooldown / same emitter
    assert len(e.process(ev(t=200.0))) == 1  # after cooldown
    assert e.process(ev(score=50.0, t=300.0)) == []  # below threshold


def test_engine_frequency_cluster_and_ack():
    e = AlertEngine([AlertRule(min_score=80, cooldown_s=60)], cluster_hz=50000)
    e.process(ev(t=0.0))
    assert e.process(ev(freq=308.02e6, t=10.0)) == []
    key = e.open_alerts()[0]["key"]
    e.acknowledge(key)
    assert e.open_alerts()[0]["state"] == "acknowledged"


def test_webhook_signs_payload():
    sent = []
    n = WebhookNotifier(
        "https://example.test/hook",
        secret="s3",
        sender=lambda url, body, headers: sent.append((url, body, headers)),
    )
    n.send({"a": 1})
    url, body, headers = sent[0]
    assert headers["X-Quietroom-Signature"] == hmac.new(b"s3", body, hashlib.sha256).hexdigest()
    assert json.loads(body) == {"a": 1}


def test_email_notifier_uses_injected_smtp():
    calls = []

    class SMTP:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def sendmail(self, frm, to, msg):
            calls.append((frm, to, msg))

    n = EmailNotifier("alerts@x.test", ["ops@x.test"], smtp_factory=SMTP)
    n.send({"node_id": "n1", "score": 90})
    assert calls and "n1" in calls[0][2]


def test_outbox_retries_then_delivers(tmp_path):
    o = Outbox(tmp_path / "o.db")
    o.add({"x": 1})
    attempts = []

    def flaky(p):
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("down")

    assert o.flush(flaky) == 0 and o.pending() == 1
    assert o.flush(flaky) == 1 and o.pending() == 0
