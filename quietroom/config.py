"""Node configuration (TOML) for unattended floor-monitoring nodes."""

from __future__ import annotations
import re
import tomllib
from dataclasses import dataclass, field
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
    fleet_base_url: str = ""
    fleet_heartbeat_s: float = 60.0
    # Free-text antenna label; part of the baseline fingerprint, so swapping
    # the antenna (and saying so here) invalidates the stored baseline.
    antenna: str = ""
    rebaseline: RebaselinePolicy = field(default_factory=RebaselinePolicy)

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
            str(fleet.get("base_url", "")),
            float(fleet.get("heartbeat_s", 60.0)),
            antenna=str(n.get("antenna", "")),
            rebaseline=rebaseline,
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
