import numpy as np
from quietroom.store import Store
from quietroom.policy import Policy, AllowEntry, BlockEntry, apply_policy
from quietroom.engine.baseline import build_baseline, drift_report
from quietroom.engine.spectrum import PowerSpectrum, Candidate


def spec(p, t=0.0):
    f = np.array([300e6, 300.025e6, 300.05e6])
    return PowerSpectrum(f, np.array(p, float), t)


def test_store_roundtrip_baseline_versions(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_node("n1", "hq", "3", "lab")
    b1 = build_baseline([spec([-90, -90, -90], 1), spec([-92, -92, -92], 2)])
    v1 = s.save_baseline("n1", "uhf", b1)
    v2 = s.save_baseline("n1", "uhf", b1)
    assert (v1, v2) == (1, 2)
    got = s.load_baseline("n1", "uhf")
    assert got.sweep_count == b1.sweep_count
    np.testing.assert_allclose(got.mean_dbm, b1.mean_dbm)


def test_store_sweeps_findings_retention_export(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_node("n1")
    s.record_sweep(
        "n1",
        "uhf",
        spec([-90, -40, -90], 100.0),
        [
            {
                "freq_hz": 300.025e6,
                "score": 80.0,
                "raw_score": 90.0,
                "band": "unknown",
                "reasons": ["x"],
            }
        ],
    )
    rows = s.findings("n1")
    assert rows[0]["freq_hz"] == 300.025e6
    assert s.export_json("n1")["findings"]
    s.record_sweep("n1", "uhf", spec([-90, -90, -90], 10.0), [])
    deleted = s.prune(now=100.0, max_age_s=50)
    assert deleted >= 1 and s.sweep_count("n1") == 1


def test_drift_report_flags_shift():
    a = build_baseline([spec([-90, -90, -90])])
    b = build_baseline([spec([-80, -90, -70])])
    r = drift_report(a, b)
    assert r["max_delta_db"] == 20.0 and r["changed_fraction"] > 0.5


def test_policy_allow_suppresses_block_escalates():
    pol = Policy([AllowEntry(300e6, 301e6, "uhf")], [BlockEntry(400e6, 401e6, "bad")])
    def c(f):
        return Candidate(f, 25e3, -50.0, 20.0)
    a = apply_policy(c(300.5e6), pol)
    assert a.action == "allow" and a.adjustment < 0
    b = apply_policy(c(400.5e6), pol)
    assert b.action == "block" and b.adjustment > 0
    n = apply_policy(c(500e6), pol)
    assert n.action == "none" and n.adjustment == 0
