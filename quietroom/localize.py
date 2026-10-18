"""Cross-node zone attribution (localization stage 1).

Honest limits: one sweeping receiver gives proximity only ("node X hears it
loudest"). With >=3 nodes hearing the same emission inside the time window, a
power-weighted centroid gives a rough zone estimate — RSSI trilateration
proper needs calibrated nodes, synchronized captures, and a path-loss/wall
model, and swept receivers rarely observe the same burst simultaneously.
Estimates therefore carry a confidence and are never presented as precise
coordinates.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class NodePosition:
    node_id: str
    x: float
    y: float
    floor: str = ""


@dataclass(frozen=True)
class Observation:
    node_id: str
    freq_hz: float
    power_dbm: float
    timestamp: float


@dataclass
class ZoneEstimate:
    zone_node: str | None
    confidence: str
    observations: int
    estimate: tuple[float, float] | None
    basis: str
    freq_hz: float | None = None


def group_observations(observations, window_s=30.0, cluster_hz=25000):
    """Group by frequency cluster; each group spans at most window_s."""
    groups = []
    for o in sorted(observations, key=lambda x: x.timestamp):
        placed = False
        for g in groups:
            if (
                abs(o.freq_hz - g[0].freq_hz) <= cluster_hz
                and o.timestamp - g[0].timestamp <= window_s
            ):
                g.append(o)
                placed = True
                break
        if not placed:
            groups.append([o])
    return groups


def attribute_zone(observations, positions, now=None, window_s=30.0, cluster_hz=25000):
    pos = {p.node_id: p for p in positions}
    groups = [
        g
        for g in group_observations(observations, window_s, cluster_hz)
        if any(o.node_id in pos for o in g)
    ]
    if not groups:
        return ZoneEstimate(None, "none", 0, None, "no observations")
    # Freshest group wins.
    g = max(groups, key=lambda grp: max(o.timestamp for o in grp))
    g = [o for o in g if o.node_id in pos]
    loudest = max(g, key=lambda o: o.power_dbm)
    if len(g) == 1:
        return ZoneEstimate(
            loudest.node_id,
            "low",
            1,
            None,
            "single-node proximity only; no position estimate",
            loudest.freq_hz,
        )
    others = [o.power_dbm for o in g if o is not loudest]
    margin = loudest.power_dbm - max(others) if others else 0.0
    # Power-weighted centroid (weights linear in power, floored).
    weights = [(10 ** (o.power_dbm / 10.0), pos[o.node_id]) for o in g]
    total = sum(w for w, _ in weights)
    est = (sum(w * p.x for w, p in weights) / total, sum(w * p.y for w, p in weights) / total)
    confidence = "high" if len(g) >= 3 and margin >= 10 else "medium" if len(g) >= 3 else "low"
    return ZoneEstimate(
        loudest.node_id,
        confidence,
        len(g),
        est,
        f"loudest node {loudest.node_id} by {margin:.0f} dB over {len(g)} nodes; "
        "weighted-centroid zone estimate (uncalibrated RSSI)",
        loudest.freq_hz,
    )
