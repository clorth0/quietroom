"""Invalid node configs fail loudly at load time, naming the key and what is allowed.

One acceptance criterion per test group:
  AC1 unknown keys rejected everywhere, with a did-you-mean when close
  AC2 [node] poll_s positive and within a sane range
  AC3 negative cooldown_s rejected; min_score / freeze_score within the 0..100 score range
  AC4 webhook_url restricted to http/https (plain http allowed with a warning)
  AC5 segment priority (and the rest of a segment) validated
  AC6 types are strict: no "false" strings for booleans, no bools for numbers
  AC7 secrets are rejected in TOML without echoing them, pointing at the env var
  AC8 every problem is reported at once, not just the first
"""

from __future__ import annotations

import copy

import pytest

from quietroom.config import ConfigError, NodeConfig, config_warnings


def doc(**sections):
    """A minimal valid document; keyword args replace or add whole sections."""
    base = {
        "node": {"id": "n1", "poll_s": 1.0},
        "segments": [{"name": "uhf", "start_hz": 300_000_000, "stop_hz": 301_000_000}],
        "store": {"path": "/tmp/q.db"},
        "alerts": {"min_score": 80, "cooldown_s": 3600},
    }
    out = copy.deepcopy(base)
    out.update(sections)
    return out


def with_(section, **kv):
    d = doc()
    if section == "segments":
        d["segments"][0].update(kv)
    else:
        d.setdefault(section, {}).update(kv)
    return d


def errors(d):
    with pytest.raises(ConfigError) as ei:
        NodeConfig.from_dict(d)
    return ei.value.issues, str(ei.value)


def test_the_minimal_document_is_valid():
    assert NodeConfig.from_dict(doc()).node_id == "n1"


def test_config_error_is_a_value_error_for_existing_callers():
    assert issubclass(ConfigError, ValueError)


# --- AC1: unknown keys -------------------------------------------------------


@pytest.mark.parametrize(
    "d, key, suggestion",
    [
        (doc(nodes={"id": "x"}), "nodes", "node"),
        (doc(segment=[{}]), "segment", "segments"),
        (doc(alert={"min_score": 80}), "alert", "alerts"),
        (with_("node", pol_s=1.0), "node.pol_s", "poll_s"),
        (with_("node", threshhold_k=4.0), "node.threshhold_k", "threshold_k"),
        (with_("segments", interval=60), "segments[0].interval", "interval_s"),
        (with_("segments", priorty=3), "segments[0].priorty", "priority"),
        (with_("store", pth="/x"), "store.pth", "path"),
        (with_("alerts", min_scor=80), "alerts.min_scor", "min_score"),
        (with_("alerts", cooldown=60), "alerts.cooldown", "cooldown_s"),
        (with_("baseline", max_age=5), "baseline.max_age", "max_age_s"),
        (with_("fleet", heartbeat=60), "fleet.heartbeat", "heartbeat_s"),
        (
            with_("alerts", email={"host": "h", "from": "a@x.test", "to": "b@x.test", "prot": 25}),
            "alerts.email.prot",
            "port",
        ),
    ],
)
def test_unknown_keys_are_rejected_with_a_did_you_mean(d, key, suggestion):
    issues, msg = errors(d)
    assert any(i.key == key for i in issues), issues
    assert f"unknown key {key!r}" in msg or f"unknown key '{key}'" in msg
    assert f"did you mean {suggestion!r}" in msg


def test_unknown_key_without_a_close_match_lists_the_allowed_keys():
    issues, msg = errors(with_("store", zzzzzz=1))
    assert "did you mean" not in msg
    assert "allowed: path" in msg


def test_unknown_key_in_a_section_lists_allowed_keys():
    _, msg = errors(with_("node", colour="red"))
    for k in ("id", "poll_s", "region", "threshold_k"):
        assert k in msg


# --- AC2: poll_s --------------------------------------------------------------


@pytest.mark.parametrize("v", [0, 0.0, -1, 0.01, 61, 3600, "fast", True])
def test_poll_s_must_be_positive_and_sane(v):
    issues, msg = errors(with_("node", poll_s=v))
    assert [i.key for i in issues] == ["node.poll_s"]
    assert "0.1" in msg and "60" in msg  # the allowed range is named


@pytest.mark.parametrize("v", [0.1, 0.5, 1, 60])
def test_poll_s_in_range_is_accepted(v):
    assert NodeConfig.from_dict(with_("node", poll_s=v)).poll_s == float(v)


# --- AC3: cooldown_s, min_score, freeze_score ----------------------------------


@pytest.mark.parametrize("v", [-1, -0.5, "1h", False])
def test_negative_or_non_numeric_cooldown_is_rejected(v):
    issues, msg = errors(with_("alerts", cooldown_s=v))
    assert [i.key for i in issues] == ["alerts.cooldown_s"]
    assert ">= 0" in msg


def test_zero_cooldown_is_allowed():
    assert NodeConfig.from_dict(with_("alerts", cooldown_s=0)).alert_cooldown_s == 0.0


@pytest.mark.parametrize("v", [-1, 100, 150, "high", None])
def test_min_score_must_be_inside_the_score_range(v):
    d = with_("alerts", min_score=v)
    if v is None:
        d["alerts"]["min_score"] = []  # TOML has no null; a wrong type instead
    issues, msg = errors(d)
    assert [i.key for i in issues] == ["alerts.min_score"]
    assert "0 <= min_score < 100" in msg


@pytest.mark.parametrize("v", [0, 50, 99.5])
def test_min_score_in_range_is_accepted(v):
    assert NodeConfig.from_dict(with_("alerts", min_score=v)).alert_min_score == float(v)


@pytest.mark.parametrize("v", [-5, 100, 250])
def test_freeze_score_shares_the_score_range(v):
    issues, _ = errors(with_("baseline", freeze_score=v))
    assert [i.key for i in issues] == ["baseline.freeze_score"]


# --- AC4: webhook_url ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://hooks.example.com/x",
        "gopher://h/",
        "javascript:alert(1)",
        "hooks.example.com/alert",  # no scheme
        "https://",  # no host
        "https:///path-only",
        "https://user:pw@hooks.example.com/x",  # credentials belong in env, not the URL
        " https://hooks.example.com/x",  # stray whitespace
    ],
)
def test_webhook_url_must_be_http_or_https_with_a_host(url):
    issues, msg = errors(with_("alerts", webhook_url=url))
    assert [i.key for i in issues] == ["alerts.webhook_url"]
    assert "http" in msg and "https" in msg
    assert "pw@" not in msg  # a credential in the URL is never echoed


@pytest.mark.parametrize("url", ["", "https://hooks.example.com/x", "HTTPS://H.example/x?a=1"])
def test_https_or_empty_webhook_is_accepted_without_warnings(url):
    cfg = NodeConfig.from_dict(with_("alerts", webhook_url=url))
    assert cfg.webhook_url == url
    assert config_warnings(cfg) == []


def test_plain_http_webhook_is_accepted_with_a_warning():
    cfg = NodeConfig.from_dict(with_("alerts", webhook_url="http://10.0.0.5:8080/hook"))
    [w] = config_warnings(cfg)
    assert "alerts.webhook_url" in w and "http://" in w and "https" in w


# --- AC5: segments ---------------------------------------------------------------


@pytest.mark.parametrize("v", [-1, 101, 2.5, "high", True])
def test_segment_priority_must_be_an_integer_0_to_100(v):
    issues, msg = errors(with_("segments", priority=v))
    assert [i.key for i in issues] == ["segments[0].priority"]
    assert "0" in msg and "100" in msg


@pytest.mark.parametrize("v", [0, 1, 100])
def test_segment_priority_in_range_is_accepted(v):
    assert NodeConfig.from_dict(with_("segments", priority=v)).segments[0].priority == v


def test_segment_mode_typo_is_rejected_not_silently_search():
    issues, msg = errors(with_("segments", mode="overveiw"))
    assert [i.key for i in issues] == ["segments[0].mode"]
    assert "'search'" in msg and "'overview'" in msg


@pytest.mark.parametrize(
    "kv, key",
    [
        ({"interval_s": 0}, "segments[0].interval_s"),
        ({"interval_s": -60}, "segments[0].interval_s"),
        ({"name": ""}, "segments[0].name"),
        ({"start_hz": "lots"}, "segments[0].start_hz"),
        ({"start": "300M"}, "segments[0].start"),  # both start and start_hz given
    ],
)
def test_other_segment_fields_are_validated(kv, key):
    issues, _ = errors(with_("segments", **kv))
    assert key in [i.key for i in issues]


def test_segment_without_stop_names_the_missing_key():
    d = doc()
    del d["segments"][0]["stop_hz"]
    issues, msg = errors(d)
    assert "segments[0].stop_hz" in [i.key for i in issues]
    assert "required" in msg


# --- AC6: strict types ------------------------------------------------------------


@pytest.mark.parametrize(
    "section, kv, key",
    [
        ("segments", {"enabled": "false"}, "segments[0].enabled"),  # bool("false") was True
        ("baseline", {"invalidate_on_fingerprint": "no"}, "baseline.invalidate_on_fingerprint"),
        ("node", {"baseline_cycles": 2.5}, "node.baseline_cycles"),
        ("node", {"baseline_cycles": True}, "node.baseline_cycles"),
        ("node", {"threshold_k": "4"}, "node.threshold_k"),
        ("node", {"region": "mars"}, "node.region"),
        ("node", {"id": 7}, "node.id"),
        ("store", {"path": ["a"]}, "store.path"),
    ],
)
def test_types_are_strict(section, kv, key):
    issues, _ = errors(with_(section, **kv))
    assert key in [i.key for i in issues]


def test_region_error_lists_the_allowed_regions():
    _, msg = errors(with_("node", region="mars"))
    assert "'eu'" in msg and "'us'" in msg


def test_a_section_that_is_not_a_table_is_rejected():
    issues, _ = errors(doc(alerts="loud"))
    assert [i.key for i in issues] == ["alerts"]


# --- AC7: secrets ----------------------------------------------------------------


@pytest.mark.parametrize(
    "d, env",
    [
        (with_("alerts", webhook_secret="s3cr3t-value"), "QUIETROOM_WEBHOOK_SECRET"),
        (with_("fleet", token="s3cr3t-value"), "QUIETROOM_CENTRAL_TOKEN"),
        (
            with_("alerts", email={"host": "h", "from": "a@x.test", "to": "b@x.test",
                                   "password": "s3cr3t-value"}),
            "QUIETROOM_SMTP_PASSWORD",
        ),
    ],
)
def test_secrets_in_toml_are_rejected_without_echoing_them(d, env):
    _, msg = errors(d)
    assert env in msg
    assert "s3cr3t-value" not in msg


# --- AC8: all problems at once -------------------------------------------------------


def test_every_problem_is_reported_in_one_error():
    d = doc()
    d["node"]["poll_s"] = 0
    d["alerts"]["min_score"] = 120
    d["alerts"]["webhook_url"] = "file:///tmp/x"
    d["segments"][0]["priority"] = -3
    d["store"]["pth"] = "/x"
    issues, msg = errors(d)
    assert sorted(i.key for i in issues) == sorted(
        [
            "node.poll_s",
            "alerts.min_score",
            "alerts.webhook_url",
            "segments[0].priority",
            "store.pth",
        ]
    )
    assert msg.count("\n  - ") == 5


# --- [fleet] outbox bounds ------------------------------------------------------------


def test_fleet_outbox_defaults():
    fc = NodeConfig.from_dict(
        with_("fleet", base_url="https://c.test", auth="legacy-token")
    ).fleet
    assert fc is not None
    assert (fc.outbox_max_rows, fc.outbox_max_age_s, fc.outbox_max_attempts) == (
        20_000, 7 * 86400.0, 5,
    )


@pytest.mark.parametrize(
    "kv, key",
    [
        ({"outbox_max_rows": 0}, "fleet.outbox_max_rows"),  # the bound cannot be switched off
        ({"outbox_max_rows": -1}, "fleet.outbox_max_rows"),
        ({"outbox_max_rows": 1.5}, "fleet.outbox_max_rows"),
        ({"outbox_max_age_s": -1}, "fleet.outbox_max_age_s"),
        ({"outbox_max_attempts": -1}, "fleet.outbox_max_attempts"),
        ({"outbox_max_attempts": True}, "fleet.outbox_max_attempts"),
        ({"heartbeat_s": 0}, "fleet.heartbeat_s"),
        ({"auth": "kerberos"}, "fleet.auth"),
        ({"base_url": "ftp://c.test"}, "fleet.base_url"),
    ],
)
def test_fleet_keys_are_validated(kv, key):
    fleet = {"base_url": "https://c.test", "auth": "legacy-token", **kv}
    issues, _ = errors(with_("fleet", **fleet))
    assert key in [i.key for i in issues]
