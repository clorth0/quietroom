"""Node configuration (TOML) for unattended floor-monitoring nodes.

Loading is strict: every key is checked against the schema below
(``quietroom.configschema``) and every problem is reported at once as a
:class:`ConfigError` naming the key and what is allowed, so a typo or a
nonsense value stops the node at start-up instead of being silently ignored.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal
from urllib.parse import urlsplit

from quietroom.bands import REGIONS
from quietroom.configschema import (
    MISSING,
    ConfigError,
    ConfigIssue,
    Field,
    check_section,
    join,
    parse_freq,
    unknown_key_issue,
)
from quietroom.engine.plan import DEFAULT_START_HZ, DEFAULT_STOP_HZ, SweepPlan
from quietroom.node.rebaseline import RebaselinePolicy
from quietroom.radio.limits import DEFAULT_BIN_HZ

__all__ = [
    "ConfigError",
    "ConfigIssue",
    "EmailConfig",
    "FleetConfig",
    "NodeConfig",
    "SegmentConfig",
    "config_warnings",
    "default_config",
    "load_config",
    "parse_freq",
]

# Score is squashed into [0, 100) by engine/score.py: 100 can never be reached,
# so a threshold of 100 would silently disable alerting.
DAY = 86400.0


def _score(default: float | None) -> Field:
    return Field("float", default, lo=0.0, hi=100.0, hi_open=True)



NODE_FIELDS = {
    "id": Field("str", "quietroom-node", nonempty=True),
    "site": Field("str", ""),
    "floor": Field("str", ""),
    "room": Field("str", ""),
    "region": Field("str", "us", choices=tuple(REGIONS), lower=True),
    "threshold_k": Field("float", 4.0, lo=0, lo_open=True),
    "baseline_cycles": Field("int", 5, lo=1, hi=1000),
    # Each loop pings the systemd watchdog (WatchdogSec=120), so a poll longer
    # than a minute risks a watchdog kill; under 0.1 s is a busy loop.
    "poll_s": Field("float", 1.0, lo=0.1, hi=60.0),
    # One wall-clock budget for the final flush of every outbox on SIGTERM. With
    # one send in flight on top it must fit the unit's TimeoutStopSec=60.
    "shutdown_flush_s": Field("float", 30.0, lo=0, lo_open=True, hi=45.0),
    "antenna": Field("str", ""),
}
SEGMENT_FIELDS = {
    "name": Field("str", nonempty=True),
    "mode": Field("str", "search", choices=("search", "overview")),
    "start_hz": Field("freq", None),
    "start": Field("freq", None),
    "stop_hz": Field("freq", None),
    "stop": Field("freq", None),
    "bin_hz": Field("freq", None),
    "bin": Field("freq", None),
    "interval_s": Field("float", 60.0, lo=0, lo_open=True),
    # Only a tiebreak among due segments (higher runs first).
    "priority": Field("int", 0, lo=0, hi=100),
    "enabled": Field("bool", True),
}
STORE_FIELDS = {"path": Field("str", "")}
ALERT_FIELDS = {
    "min_score": _score(80.0),
    "cooldown_s": Field("float", 3600.0, lo=0),
    "webhook_url": Field("url", "", schemes=("http", "https")),
    "max_attempts": Field("int", 5, lo=0, note="(0 = never)"),
    "max_age_s": Field("float", 7 * DAY, lo=0, note="(0 = never)"),
    # Per channel, per tick: no new send starts after this many seconds.
    "flush_budget_s": Field("float", 15.0, lo=0, lo_open=True, hi=30.0),
}
BASELINE_FIELDS = {
    "max_age_s": Field("float", 0.0, lo=0, note="(0 = never)"),
    "drift_window": Field("int", 0, lo=2, also=(0,), note="(0 = off)"),
    "drift_mean_db": Field("float", 3.0, lo=0, lo_open=True),
    "drift_changed_fraction": Field("float", 0.10, lo=0, lo_open=True, hi=1.0),
    "freeze_score": _score(None),
    "incident_hold_s": Field("float", 3600.0, lo=0),
    "invalidate_on_fingerprint": Field("bool", True),
}
EMAIL_FIELDS = {
    "host": Field("str", nonempty=True),
    "port": Field("int", None, lo=1, hi=65535),
    "starttls": Field("bool", None),
    "ssl": Field("bool", False),
    "from": Field("str"),
    "to": Field("addresses"),
    "username": Field("str", ""),
    "ca_file": Field("str", ""),
    "timeout_s": Field("float", 10.0, lo=0, lo_open=True),
}
FLEET_FIELDS = {
    "base_url": Field("url", "", schemes=("http", "https")),
    "heartbeat_s": Field("float", 60.0, lo=0, lo_open=True),
    "auth": Field("str", "mtls", choices=("mtls", "legacy-token")),
    "cert_file": Field("str", ""),
    "key_file": Field("str", ""),
    "ca_file": Field("str", ""),
    "identity_path": Field("str", ""),
    "cert_warn_days": Field("float", 30.0, lo=0, note="(0 = no warning)"),
    # The node-side queue of undelivered uploads. The row bound cannot be
    # switched off: a node cut off from central must not fill its disk.
    "outbox_max_rows": Field("int", 20_000, lo=1),
    "outbox_max_age_s": Field("float", 7 * DAY, lo=0, note="(0 = never)"),
    "outbox_max_attempts": Field("int", 5, lo=0, note="(0 = never)"),
    # Per tick: no new upload starts after this many seconds.
    "flush_budget_s": Field("float", 15.0, lo=0, lo_open=True, hi=30.0),
}
SECTIONS = ("node", "segments", "store", "alerts", "baseline", "fleet")


def _raise(issues: list[ConfigIssue]) -> None:
    if issues:
        raise ConfigError(issues)


@dataclass(frozen=True)
class SegmentConfig:
    name: str
    plan: SweepPlan
    interval_s: float = 60.0
    priority: int = 0
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: Any) -> SegmentConfig:
        issues: list[ConfigIssue] = []
        seg = cls._parse(d, "segments[0]", issues)
        _raise(issues)
        assert seg is not None
        return seg

    @classmethod
    def _parse(cls, d: Any, prefix: str, issues: list[ConfigIssue]) -> SegmentConfig | None:
        n = len(issues)
        v = check_section(prefix, d, SEGMENT_FIELDS, issues)
        hz: dict[str, int] = {}
        for long, short, default in (
            ("start_hz", "start", MISSING),
            ("stop_hz", "stop", MISSING),
            ("bin_hz", "bin", 25_000),
        ):
            if not isinstance(d, dict):
                break
            if long in d and short in d:
                issues.append(
                    ConfigIssue(join(prefix, short), f"give either {long} or {short}, not both")
                )
            elif long not in d and short not in d:
                if default is MISSING:
                    issues.append(
                        ConfigIssue(join(prefix, long), f"is required ({long} or {short})")
                    )
                else:
                    hz[long] = default
            else:
                got = v.get(long) if long in d else v.get(short)
                if got is not None:
                    hz[long] = got
        if len(issues) > n:
            return None
        make = SweepPlan.overview if v["mode"] == "overview" else SweepPlan.search
        try:
            plan = make(hz["start_hz"], hz["stop_hz"], hz["bin_hz"])
        except ValueError as e:
            issues.append(ConfigIssue(prefix, f"invalid sweep: {e}"))
            return None
        return cls(v["name"], plan, v["interval_s"], v["priority"], v["enabled"])


def _rebaseline_policy(
    b: Any, alert_min_score: float, issues: list[ConfigIssue]
) -> RebaselinePolicy:
    n = len(issues)
    v = check_section("baseline", b, BASELINE_FIELDS, issues)
    if len(issues) > n:
        return RebaselinePolicy()
    try:
        return RebaselinePolicy(
            max_age_s=v["max_age_s"] or None,
            drift_window=v["drift_window"],
            drift_mean_db=v["drift_mean_db"],
            drift_changed_fraction=v["drift_changed_fraction"],
            freeze_score=alert_min_score if v["freeze_score"] is None else v["freeze_score"],
            incident_hold_s=v["incident_hold_s"],
            invalidate_on_fingerprint=v["invalidate_on_fingerprint"],
        )
    except ValueError as e:  # pragma: no cover - the schema covers the same rules
        issues.append(ConfigIssue("baseline", str(e)))
        return RebaselinePolicy()


_ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")


def _address(v: str, key: str, issues: list[ConfigIssue]) -> str:
    if not _ADDR_RE.match(v):
        # An address is not a secret; echoing it helps spot the typo.
        issues.append(ConfigIssue(key, f"invalid address {v!r}"))
    return v


@dataclass(frozen=True)
class EmailConfig:
    """[alerts.email]: SMTP delivery. The password is never in TOML; it comes
    from the QUIETROOM_SMTP_PASSWORD environment variable at startup."""

    host: str
    from_addr: str
    to_addrs: tuple[str, ...]
    port: int = 587
    starttls: bool = True
    ssl: bool = False  # implicit TLS (SMTPS, usually port 465)
    username: str = ""
    ca_file: str = ""  # private relay CA; default = system trust store
    timeout_s: float = 10.0

    @classmethod
    def from_dict(cls, d: Any) -> EmailConfig:
        issues: list[ConfigIssue] = []
        cfg = cls._parse(d, issues)
        _raise(issues)
        assert cfg is not None
        return cfg

    @classmethod
    def _parse(cls, d: Any, issues: list[ConfigIssue]) -> EmailConfig | None:
        p = "alerts.email"
        n = len(issues)
        v = check_section(p, d, EMAIL_FIELDS, issues)
        if "from" in v:
            _address(v["from"], f"{p}.from", issues)
        for a in v.get("to", []):
            _address(a, f"{p}.to", issues)
        ssl_on = v.get("ssl", False)
        starttls = (not ssl_on) if v.get("starttls") is None else v["starttls"]
        if ssl_on and starttls:
            issues.append(ConfigIssue(f"{p}.ssl", "set ssl or starttls, not both"))
        if v.get("username") and not (ssl_on or starttls):
            issues.append(
                ConfigIssue(
                    f"{p}.username",
                    "username set without starttls/ssl: refusing plaintext AUTH",
                )
            )
        if len(issues) > n:
            return None
        return cls(
            host=v["host"],
            from_addr=v["from"],
            to_addrs=tuple(v["to"]),
            port=v["port"] if v["port"] is not None else (465 if ssl_on else 587),
            starttls=starttls,
            ssl=ssl_on,
            username=v["username"],
            ca_file=v["ca_file"],
            timeout_s=v["timeout_s"],
        )


FleetAuth = Literal["mtls", "legacy-token"]


@dataclass(frozen=True)
class FleetConfig:
    """``[fleet]``: central upload. Auth is per-node mTLS unless legacy is named.

    ``auth = "mtls"`` (default): ``cert_file``/``key_file`` are this node's
    client certificate (identity = its UUID, which must match ``identity_path``),
    ``ca_file`` pins the CA that issued central's server certificate.
    ``auth = "legacy-token"``: DEPRECATED shared bearer from the
    ``QUIETROOM_CENTRAL_TOKEN`` env var; ``ca_file`` optionally pins central.
    ``cert_warn_days``: warn (health, heartbeat) this long before the client
    certificate expires; 0 disables the warning.
    ``outbox_max_*``: bounds on the local queue of undelivered uploads (see
    ``quietroom.fleet.FleetClient``).
    """

    base_url: str
    heartbeat_s: float = 60.0
    auth: FleetAuth = "mtls"
    cert_file: str = ""
    key_file: str = ""
    ca_file: str = ""
    identity_path: str = ""
    cert_warn_days: float = 30.0
    outbox_max_rows: int = 20_000
    outbox_max_age_s: float = 7 * DAY
    outbox_max_attempts: int = 5
    flush_budget_s: float = 15.0

    @classmethod
    def from_dict(cls, d: Any, store_path: str = "") -> FleetConfig | None:
        issues: list[ConfigIssue] = []
        cfg = cls._parse(d, store_path, issues)
        _raise(issues)
        return cfg

    @classmethod
    def _parse(cls, d: Any, store_path: str, issues: list[ConfigIssue]) -> FleetConfig | None:
        n = len(issues)
        v = check_section("fleet", d, FLEET_FIELDS, issues)
        if len(issues) > n or not v["base_url"]:
            return None
        base, auth = v["base_url"], v["auth"]
        identity = v["identity_path"]
        if not identity and store_path:
            identity = str(PurePath(store_path).with_name("identity.json"))
        files = {k: v[k] for k in ("cert_file", "key_file", "ca_file")}
        if auth == "mtls":
            missing = [k for k, f in files.items() if not f]
            if missing:
                issues.append(
                    ConfigIssue(
                        "fleet.auth",
                        f"auth = 'mtls' (the default) needs {', '.join(missing)}. "
                        "Issue this node a client certificate for its UUID "
                        "(quietroom-node --print-identity); to keep the deprecated shared "
                        'bearer token meanwhile, set auth = "legacy-token" explicitly',
                    )
                )
            if urlsplit(base).scheme.lower() != "https":
                issues.append(
                    ConfigIssue("fleet.base_url", "mTLS needs an https:// base_url")
                )
            if not identity:
                issues.append(
                    ConfigIssue(
                        "fleet.identity_path", "mTLS needs identity_path (or a [store] path)"
                    )
                )
        if len(issues) > n:
            return None
        return cls(
            base,
            v["heartbeat_s"],
            "mtls" if auth == "mtls" else "legacy-token",
            identity_path=identity,
            cert_warn_days=v["cert_warn_days"],
            outbox_max_rows=v["outbox_max_rows"],
            outbox_max_age_s=v["outbox_max_age_s"],
            outbox_max_attempts=v["outbox_max_attempts"],
            flush_budget_s=v["flush_budget_s"],
            **files,
        )


@dataclass(frozen=True)
class NodeConfig:
    node_id: str = "quietroom-node"
    site: str = ""
    floor: str = ""
    room: str = ""
    region: str = "us"
    threshold_k: float = 4.0
    baseline_cycles: int = 5
    poll_s: float = 1.0
    segments: tuple[SegmentConfig, ...] = ()
    store_path: str = ""
    alert_min_score: float = 80.0
    alert_cooldown_s: float = 3600.0
    webhook_url: str = ""
    fleet: FleetConfig | None = None
    # Free-text antenna label; part of the baseline fingerprint, so swapping
    # the antenna (and saying so here) invalidates the stored baseline.
    antenna: str = ""
    rebaseline: RebaselinePolicy = field(default_factory=RebaselinePolicy)
    email: EmailConfig | None = None
    # Dead letters: an alert failing this many times stops holding its channel
    # (and is parked once the channel delivers others); 0 = never.
    alert_max_attempts: int = 5
    # Park any alert undelivered for this long; 0 = never.
    alert_max_age_s: float = 7 * DAY
    # Seconds per alert-channel flush (each tick) before no new send starts.
    alert_flush_budget_s: float = 15.0
    # One budget for the final flush of all outboxes when the daemon stops.
    shutdown_flush_s: float = 30.0

    @property
    def fleet_base_url(self) -> str:
        return self.fleet.base_url if self.fleet else ""

    @property
    def fleet_heartbeat_s(self) -> float:
        return self.fleet.heartbeat_s if self.fleet else 60.0

    @classmethod
    def from_dict(cls, d: Any) -> NodeConfig:
        """Validate a parsed TOML document; ConfigError lists every problem."""
        issues: list[ConfigIssue] = []
        if not isinstance(d, dict):
            raise ConfigError([ConfigIssue("", "config must be a TOML table")])
        for k in d:
            if k not in SECTIONS:
                issues.append(unknown_key_issue(k, k, list(SECTIONS)))
        n = check_section("node", d.get("node", {}), NODE_FIELDS, issues)
        segs = cls._segments(d.get("segments", []), issues)
        store = check_section("store", d.get("store", {}), STORE_FIELDS, issues)
        raw_alerts = d.get("alerts", {})
        alerts = check_section("alerts", raw_alerts, ALERT_FIELDS, issues, subtables=("email",))
        fleet = FleetConfig._parse(d.get("fleet", {}), store.get("path", ""), issues)
        rebaseline = _rebaseline_policy(
            d.get("baseline", {}), alerts.get("min_score", 80.0), issues
        )
        email = None
        if isinstance(raw_alerts, dict) and "email" in raw_alerts:
            email = EmailConfig._parse(raw_alerts["email"], issues)
        _raise(issues)
        return cls(
            n["id"],
            n["site"],
            n["floor"],
            n["room"],
            n["region"],
            n["threshold_k"],
            n["baseline_cycles"],
            n["poll_s"],
            segs,
            store["path"],
            alerts["min_score"],
            alerts["cooldown_s"],
            alerts["webhook_url"],
            fleet,
            antenna=n["antenna"],
            rebaseline=rebaseline,
            email=email,
            alert_max_attempts=alerts["max_attempts"],
            alert_max_age_s=alerts["max_age_s"],
            alert_flush_budget_s=alerts["flush_budget_s"],
            shutdown_flush_s=n["shutdown_flush_s"],
        )

    @staticmethod
    def _segments(raw: Any, issues: list[ConfigIssue]) -> tuple[SegmentConfig, ...]:
        if not isinstance(raw, list):
            issues.append(
                ConfigIssue("segments", "must be an array of tables ([[segments]] blocks)")
            )
            return ()
        if not raw:
            issues.append(
                ConfigIssue("segments", "config needs at least one [[segments]] table")
            )
            return ()
        segs = []
        for i, s in enumerate(raw):
            seg = SegmentConfig._parse(s, f"segments[{i}]", issues)
            if seg is not None:
                segs.append(seg)
        seen: set[str] = set()
        for i, s in enumerate(segs):
            if s.name in seen:
                issues.append(
                    ConfigIssue(f"segments[{i}].name", f"segment names must be unique: {s.name!r}")
                )
            seen.add(s.name)
        return tuple(segs)

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "site": self.site,
            "floor": self.floor,
            "room": self.room,
            "region": self.region,
            "segments": [s.name for s in self.segments],
        }


# Warn when the worst case leaves less than ~30 s of WatchdogSec=120 for the sweep
# itself, or less than 5 s of TimeoutStopSec=60 for closing the files.
TICK_FLUSH_WARN_S = 90.0
STOP_FLUSH_WARN_S = 55.0


def config_warnings(cfg: NodeConfig) -> list[str]:
    """Valid but probably unintended settings, for --check-config and start-up."""
    out = []
    if urlsplit(cfg.webhook_url).scheme.lower() == "http":
        out.append(
            "alerts.webhook_url uses plain http://: alert contents and the HMAC-signed "
            "body cross the network unencrypted and unauthenticated; use https:// "
            "unless the receiver is on a trusted local network"
        )
    fc = cfg.fleet
    if fc is not None and fc.auth == "legacy-token" and (
        urlsplit(fc.base_url).scheme.lower() == "http"
    ):
        out.append(
            "fleet.base_url uses plain http:// with auth = \"legacy-token\": the shared "
            "QUIETROOM_CENTRAL_TOKEN crosses the network in clear; use https://"
        )
    # Worst cases: each sink spends its whole budget and then waits out one send
    # timeout (the send in flight). Webhook and fleet uploads time out at 10 s.
    sends: list[tuple[str, float, float]] = []  # (sink, budget, send timeout)
    if cfg.email is not None:
        sends.append(("email", cfg.alert_flush_budget_s, cfg.email.timeout_s))
    if cfg.webhook_url:
        sends.append(("webhook", cfg.alert_flush_budget_s, 10.0))
    if fc is not None:
        sends.append(("fleet", fc.flush_budget_s, 10.0))
    tick = sum(b + t for _, b, t in sends)
    if tick > TICK_FLUSH_WARN_S:
        out.append(
            f"a tick's outbox flushes can take up to {tick:g} s ("
            + ", ".join(f"{n} {b:g}+{t:g}" for n, b, t in sends)
            + " s), close to the systemd watchdog (WatchdogSec=120); lower "
            "alerts.flush_budget_s, fleet.flush_budget_s or alerts.email.timeout_s"
        )
    worst_send = max((t for _, _, t in sends), default=0.0)
    stop = cfg.shutdown_flush_s + worst_send
    if stop > STOP_FLUSH_WARN_S:
        out.append(
            f"stopping can take up to {stop:g} s (node.shutdown_flush_s = "
            f"{cfg.shutdown_flush_s:g} plus one {worst_send:g} s send in flight), close to "
            "the unit's TimeoutStopSec=60, after which systemd kills the node mid-send"
        )
    late = [
        f"{s.name!r} (interval_s = {s.interval_s:g})"
        for s in cfg.segments
        if s.enabled and s.interval_s < cfg.poll_s
    ]
    if late:
        out.append(
            f"node.poll_s = {cfg.poll_s:g} is longer than the interval of segment(s) "
            f"{', '.join(late)}; they will run at most every {cfg.poll_s:g} s"
        )
    return out


def load_config(path):
    """Read and validate a TOML config; ConfigError (a ValueError) names the file."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([ConfigIssue("", f"not valid TOML: {e}")], str(path)) from None
    try:
        return NodeConfig.from_dict(data)
    except ConfigError as e:
        raise e.with_source(str(path)) from None


def demo_document(node_id: str = "demo-node") -> dict[str, Any]:
    """The --demo config as a TOML-shaped document (validated like any file)."""
    return {
        "node": {"id": node_id},
        "segments": [
            {
                "name": "covert-uhf",
                "start_hz": DEFAULT_START_HZ,
                "stop_hz": DEFAULT_STOP_HZ,
                "bin_hz": DEFAULT_BIN_HZ,
                "interval_s": 60,
                "priority": 10,
            },
            {
                "name": "wifi24",
                "mode": "overview",
                "start": "2.4G",
                "stop": "2.4835G",
                "bin": "100k",
                "interval_s": 300,
                "priority": 5,
            },
        ],
    }


def default_config(node_id="demo-node"):
    return NodeConfig.from_dict(demo_document(node_id))
