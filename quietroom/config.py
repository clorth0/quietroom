"""Node configuration (TOML) for unattended floor-monitoring nodes."""

from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Literal
from quietroom.bands import REGIONS
from quietroom.engine.plan import SweepPlan
from quietroom.node.rebaseline import RebaselinePolicy

_FREQ_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:hz)?\s*$", re.I)
_SCALE = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}


def parse_freq(v):
    if isinstance(v, int):
        return v
    m = _FREQ_RE.match(str(v))
    if not m:
        raise ValueError(f"invalid frequency {v!r}")
    return int(round(float(m.group(1)) * _SCALE[m.group(2).lower()]))


@dataclass(frozen=True)
class SegmentConfig:
    name: str
    plan: SweepPlan
    interval_s: float = 60.0
    priority: int = 0
    enabled: bool = True

    @classmethod
    def from_dict(cls, d):
        if not d.get("name"):
            raise ValueError("segment needs a name")
        interval = float(d.get("interval_s", 60))
        if interval <= 0:
            raise ValueError(f"segment {d['name']}: interval_s must be > 0")
        make = (
            SweepPlan.overview if str(d.get("mode", "search")) == "overview" else SweepPlan.search
        )
        plan = make(
            parse_freq(d["start_hz"] if "start_hz" in d else d["start"]),
            parse_freq(d["stop_hz"] if "stop_hz" in d else d["stop"]),
            parse_freq(d.get("bin_hz", d.get("bin", 25000))),
        )
        return cls(
            str(d["name"]), plan, interval, int(d.get("priority", 0)), bool(d.get("enabled", True))
        )


_BASELINE_KEYS = {
    "max_age_s",
    "drift_window",
    "drift_mean_db",
    "drift_changed_fraction",
    "freeze_score",
    "incident_hold_s",
    "invalidate_on_fingerprint",
}


def _rebaseline_policy(b, alert_min_score):
    unknown = set(b) - _BASELINE_KEYS
    if unknown:
        raise ValueError(f"unknown [baseline] keys: {', '.join(sorted(unknown))}")
    max_age = b.get("max_age_s")
    return RebaselinePolicy(
        max_age_s=None if max_age in (None, 0) else float(max_age),
        drift_window=int(b.get("drift_window", 0)),
        drift_mean_db=float(b.get("drift_mean_db", 3.0)),
        drift_changed_fraction=float(b.get("drift_changed_fraction", 0.10)),
        freeze_score=float(b.get("freeze_score", alert_min_score)),
        incident_hold_s=float(b.get("incident_hold_s", 3600.0)),
        invalidate_on_fingerprint=bool(b.get("invalidate_on_fingerprint", True)),
    )
_ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+$")
_EMAIL_KEYS = {
    "host", "port", "starttls", "ssl", "from", "to", "username", "ca_file", "timeout_s",
}


def _address(v, what):
    v = str(v)
    if not _ADDR_RE.match(v):
        raise ValueError(f"[alerts.email] {what}: invalid address {v!r}")
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
    def from_dict(cls, d):
        if "password" in d:
            raise ValueError(
                "[alerts.email] password must not be in the config file; "
                "set QUIETROOM_SMTP_PASSWORD in the service environment"
            )
        unknown = set(d) - _EMAIL_KEYS
        if unknown:
            raise ValueError(f"unknown [alerts.email] keys: {', '.join(sorted(unknown))}")
        if not d.get("host"):
            raise ValueError("[alerts.email] needs a host")
        if not d.get("from"):
            raise ValueError("[alerts.email] needs a from address")
        to = d.get("to")
        to_list = [to] if isinstance(to, str) else list(to or [])
        if not to_list:
            raise ValueError("[alerts.email] needs at least one to address")
        ssl_on = bool(d.get("ssl", False))
        starttls = bool(d.get("starttls", not ssl_on))
        if ssl_on and starttls:
            raise ValueError("[alerts.email] set ssl or starttls, not both")
        port = int(d.get("port", 465 if ssl_on else 587))
        if not 0 < port < 65536:
            raise ValueError(f"[alerts.email] port {port} out of range")
        username = str(d.get("username", ""))
        if username and not (ssl_on or starttls):
            raise ValueError(
                "[alerts.email] username set without starttls/ssl: refusing plaintext AUTH"
            )
        timeout = float(d.get("timeout_s", 10.0))
        if timeout <= 0:
            raise ValueError("[alerts.email] timeout_s must be > 0")
        return cls(
            host=str(d["host"]),
            from_addr=_address(d["from"], "from"),
            to_addrs=tuple(_address(a, "to") for a in to_list),
            port=port,
            starttls=starttls,
            ssl=ssl_on,
            username=username,
            ca_file=str(d.get("ca_file", "")),
            timeout_s=timeout,
        )


FleetAuth = Literal["mtls", "legacy-token"]
_FLEET_KEYS = frozenset(
    {"base_url", "heartbeat_s", "auth", "cert_file", "key_file", "ca_file", "identity_path"}
)


@dataclass(frozen=True)
class FleetConfig:
    """``[fleet]``: central upload. Auth is per-node mTLS unless legacy is named.

    ``auth = "mtls"`` (default): ``cert_file``/``key_file`` are this node's
    client certificate (identity = its UUID, which must match ``identity_path``),
    ``ca_file`` pins the CA that issued central's server certificate.
    ``auth = "legacy-token"``: DEPRECATED shared bearer from the
    ``QUIETROOM_CENTRAL_TOKEN`` env var; ``ca_file`` optionally pins central.
    """

    base_url: str
    heartbeat_s: float = 60.0
    auth: FleetAuth = "mtls"
    cert_file: str = ""
    key_file: str = ""
    ca_file: str = ""
    identity_path: str = ""

    @classmethod
    def from_dict(cls, d, store_path: str = "") -> FleetConfig | None:
        if "token" in d:
            raise ValueError(
                "[fleet] token must not be stored in the config; use the "
                "QUIETROOM_CENTRAL_TOKEN environment variable (legacy-token auth only)"
            )
        unknown = set(d) - _FLEET_KEYS
        if unknown:
            raise ValueError(f"[fleet] unknown keys: {sorted(unknown)}")
        base = str(d.get("base_url", ""))
        if not base:
            return None
        auth = str(d.get("auth", "mtls"))
        if auth not in ("mtls", "legacy-token"):
            raise ValueError(f"[fleet] auth must be 'mtls' or 'legacy-token', got {auth!r}")
        heartbeat = float(d.get("heartbeat_s", 60.0))
        if heartbeat <= 0:
            raise ValueError("[fleet] heartbeat_s must be > 0")
        files = {k: str(d.get(k, "")) for k in ("cert_file", "key_file", "ca_file")}
        identity = str(d.get("identity_path", ""))
        if not identity and store_path:
            identity = str(PurePath(store_path).with_name("identity.json"))
        if auth == "mtls":
            missing = [k for k, v in files.items() if not v]
            if missing:
                raise ValueError(
                    f"[fleet] auth = 'mtls' (the default) needs {', '.join(missing)}. "
                    "Issue this node a client certificate for its UUID "
                    "(quietroom-node --print-identity); to keep the deprecated shared "
                    'bearer token meanwhile, set auth = "legacy-token" explicitly'
                )
            if not base.lower().startswith("https://"):
                raise ValueError(f"[fleet] mTLS needs an https:// base_url, got {base!r}")
            if not identity:
                raise ValueError("[fleet] mTLS needs identity_path (or a [store] path)")
        return cls(
            base, heartbeat, "mtls" if auth == "mtls" else "legacy-token", identity_path=identity,
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
    alert_max_age_s: float = 7 * 86400.0

    @property
    def fleet_base_url(self) -> str:
        return self.fleet.base_url if self.fleet else ""

    @property
    def fleet_heartbeat_s(self) -> float:
        return self.fleet.heartbeat_s if self.fleet else 60.0

    @classmethod
    def from_dict(cls, d):
        n = d.get("node", {})
        region = str(n.get("region", "us")).lower()
        if region not in REGIONS:
            raise ValueError(f"unknown region {region!r}")
        segs = tuple(SegmentConfig.from_dict(x) for x in d.get("segments", []))
        if not segs:
            raise ValueError("config needs at least one [[segments]] entry")
        names = [s.name for s in segs]
        if len(set(names)) != len(names):
            raise ValueError("segment names must be unique")
        k = float(n.get("threshold_k", 4.0))
        if k <= 0:
            raise ValueError("threshold_k must be > 0")
        cycles = int(n.get("baseline_cycles", 5))
        if cycles < 1:
            raise ValueError("baseline_cycles must be >= 1")
        store = d.get("store", {})
        alerts = d.get("alerts", {})
        fleet = d.get("fleet", {})
        alert_min = float(alerts.get("min_score", 80.0))
        rebaseline = _rebaseline_policy(d.get("baseline", {}), alert_min)
        return cls(
            str(n.get("id", "quietroom-node")),
            str(n.get("site", "")),
            str(n.get("floor", "")),
            str(n.get("room", "")),
            region,
            k,
            cycles,
            float(n.get("poll_s", 1.0)),
            segs,
            str(store.get("path", "")),
            alert_min,
            float(alerts.get("cooldown_s", 3600.0)),
            str(alerts.get("webhook_url", "")),
            FleetConfig.from_dict(fleet, str(store.get("path", ""))),
            antenna=str(n.get("antenna", "")),
            rebaseline=rebaseline,
            email=EmailConfig.from_dict(alerts["email"]) if "email" in alerts else None,
            alert_max_attempts=_alert_max_attempts(alerts),
            alert_max_age_s=_alert_max_age(alerts),
        )

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "site": self.site,
            "floor": self.floor,
            "room": self.room,
            "region": self.region,
            "segments": [s.name for s in self.segments],
        }


def _alert_max_attempts(alerts):
    v = alerts.get("max_attempts", 5)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"[alerts] max_attempts must be an integer >= 0 (0 = never), got {v!r}")
    return v


def _alert_max_age(alerts):
    v = alerts.get("max_age_s", 7 * 86400.0)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError(f"[alerts] max_age_s must be a number >= 0 (0 = never), got {v!r}")
    return float(v)


def load_config(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return NodeConfig.from_dict(data)


def default_config(node_id="demo-node"):
    return NodeConfig(
        node_id=node_id,
        segments=(
            SegmentConfig("covert-uhf", SweepPlan.search(), 60, 10),
            SegmentConfig(
                "wifi24",
                SweepPlan.overview(parse_freq("2.4G"), parse_freq("2.4835G"), parse_freq("100k")),
                300,
                5,
            ),
        ),
    )
