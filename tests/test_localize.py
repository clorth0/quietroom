from quietroom.localize import NodePosition, Observation, attribute_zone, group_observations

NODES = [NodePosition("a", 0, 0, "3"), NodePosition("b", 10, 0, "3"), NodePosition("c", 0, 10, "3")]


def test_loudest_node_zone_and_confidence():
    obs = [
        Observation("a", 308e6, -30, 100.0),
        Observation("b", 308.01e6, -60, 102.0),
        Observation("c", 308e6, -70, 101.0),
    ]
    z = attribute_zone(obs, NODES, now=110.0)
    assert z.zone_node == "a" and z.confidence == "high" and z.estimate is not None
    assert 0 <= z.estimate[0] < 5


def test_single_node_is_proximity_only():
    z = attribute_zone([Observation("a", 308e6, -30, 100.0)], NODES, now=105.0)
    assert z.zone_node == "a" and z.confidence == "low" and z.estimate is None
    assert "proximity" in z.basis


def test_stale_or_mismatched_freq_not_grouped():
    obs = [Observation("a", 308e6, -30, 0.0), Observation("b", 500e6, -30, 100.0)]
    groups = group_observations(obs, window_s=10, cluster_hz=25000)
    assert len(groups) == 2
    z = attribute_zone(obs, NODES, now=100.0)
    assert z.observations == 1
