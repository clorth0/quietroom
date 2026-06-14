import numpy as np
from quietroom.engine.spectrum import (
    PowerSpectrum, Baseline, Candidate, DetectorResult, Finding,
)


def test_power_spectrum_holds_arrays():
    ps = PowerSpectrum(
        freqs_hz=np.array([1.0, 2.0, 3.0]),
        power_dbm=np.array([-90.0, -80.0, -85.0]),
        timestamp=123.0,
    )
    assert ps.power_dbm[1] == -80.0
    assert len(ps.freqs_hz) == 3


def test_candidate_and_finding_compose():
    cand = Candidate(
        center_freq_hz=433_000_000.0,
        bandwidth_hz=20_000.0,
        peak_power_dbm=-40.0,
        snr_over_baseline_db=18.0,
    )
    finding = Finding(
        candidate=cand,
        score=72.0,
        band_label="unknown",
        reasons=["narrowband carrier"],
        breakdown={"signatures": 15.0},
    )
    assert finding.candidate.center_freq_hz == 433_000_000.0
    assert finding.score == 72.0
    assert finding.breakdown["signatures"] == 15.0


def test_detector_result_defaults_reasons_list():
    r = DetectorResult(name="catalog", contribution=30.0, reasons=["x"])
    assert r.name == "catalog"
    assert r.reasons == ["x"]
