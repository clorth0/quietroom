import numpy as np

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.engine.baseline import build_baseline
from quietroom.engine.pipeline import sweep_findings


def _flat(power_dbm, t=0.0):
    freqs = np.arange(300e6, 300e6 + 10e3 * 20, 10e3)  # 20 bins around 300 MHz
    return PowerSpectrum(
        freqs_hz=freqs,
        power_dbm=np.full(20, power_dbm, dtype=float),
        timestamp=t,
    )


def test_clean_room_returns_no_findings():
    base = build_baseline([_flat(-95), _flat(-95), _flat(-95)])
    live = _flat(-95)
    assert sweep_findings(live, base) == []


def test_planted_bug_surfaces_as_top_finding():
    base = build_baseline([_flat(-95), _flat(-95), _flat(-95)])
    live = _flat(-95)
    # Plant a strong narrowband carrier at an uncatalogued, covert-band freq.
    live.power_dbm[8] = -30.0  # bin 8 => 300e6 + 8*10e3 = 300.08 MHz
    findings = sweep_findings(live, base)
    assert len(findings) >= 1
    top = findings[0]
    assert top.score > 50.0
    assert "not in any known band" in top.reasons
    assert "narrowband carrier" in top.reasons


def test_findings_sorted_by_score_descending():
    base = build_baseline([_flat(-95), _flat(-95), _flat(-95)])
    live = _flat(-95)
    live.power_dbm[3] = -35.0
    live.power_dbm[15] = -80.0  # weaker excess
    findings = sweep_findings(live, base)
    scores = [f.score for f in findings]
    assert scores == sorted(scores, reverse=True)
