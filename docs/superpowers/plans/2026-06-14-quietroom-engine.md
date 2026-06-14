# Quietroom Detection Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Quietroom's pure, hardware-independent detection engine: the data types, band catalog, baseline builder, four detectors, and scoring fusion that turn sweep data into a ranked list of suspicious emitters.

**Architecture:** A standalone Python package `quietroom.engine` plus a `quietroom.bands` catalog. Nothing here imports Flask or talks to a radio. Functions take spectrum/IQ-derived arrays in and return findings out, so the whole engine is unit-tested against synthetic and recorded fixtures. The radio, audio, and web layers (next plan) call into this engine.

**Tech Stack:** Python 3.11+, uv, hatchling, numpy, scipy, pytest.

---

## File Structure

- `pyproject.toml` — project + deps (numpy, scipy; pytest dev extra)
- `quietroom/__init__.py` — package marker
- `quietroom/bands.py` — `Band`, `BANDS` table, `label_for()`
- `quietroom/engine/__init__.py` — package marker
- `quietroom/engine/spectrum.py` — dataclasses: `PowerSpectrum`, `Baseline`, `Candidate`, `DetectorResult`, `Finding`
- `quietroom/engine/baseline.py` — `build_baseline()`, `zscores()`
- `quietroom/engine/detectors/__init__.py` — package marker
- `quietroom/engine/detectors/diff.py` — `find_candidates()`
- `quietroom/engine/detectors/catalog.py` — `score_band()`
- `quietroom/engine/detectors/signatures.py` — `score_signatures()`
- `quietroom/engine/detectors/audio_corr.py` — `envelope_correlation()`, `score_audio()`
- `quietroom/engine/score.py` — `score_candidate()`
- `tests/` — one test module per source module, plus `tests/test_pipeline.py`

Each file has one responsibility. Detectors are independent pure functions returning a uniform `DetectorResult`, so `score.py` fuses them without knowing their internals.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `quietroom/__init__.py`
- Create: `quietroom/engine/__init__.py`
- Create: `quietroom/engine/detectors/__init__.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

`tests/test_smoke.py`:
```python
def test_package_imports():
    import quietroom
    import quietroom.engine
    assert quietroom.engine is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: FAIL (collection error: `ModuleNotFoundError: No module named 'quietroom'`)

- [ ] **Step 3: Create the project files**

`pyproject.toml`:
```toml
[project]
name = "quietroom"
version = "0.1.0"
description = "Receive-only HackRF TSCM bug-sweep tool"
requires-python = ">=3.11"
dependencies = [
    "numpy>=2.0",
    "scipy>=1.11",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["quietroom"]
```

`quietroom/__init__.py`:
```python
"""Quietroom: receive-only HackRF TSCM bug-sweep tool."""
```

`quietroom/engine/__init__.py`:
```python
"""Pure, hardware-independent detection engine."""
```

`quietroom/engine/detectors/__init__.py`:
```python
"""Independent detectors that each return a DetectorResult."""
```

- [ ] **Step 4: Sync and run the test to verify it passes**

Run: `uv sync --extra dev && uv run pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock quietroom/ tests/test_smoke.py
git commit -m "feat: scaffold quietroom package and engine"
```

---

### Task 2: Core data types

**Files:**
- Create: `quietroom/engine/spectrum.py`
- Test: `tests/test_spectrum.py`

- [ ] **Step 1: Write the failing test**

`tests/test_spectrum.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spectrum.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.spectrum'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/spectrum.py`:
```python
"""Core data types for the detection engine."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class PowerSpectrum:
    """One sweep result: power versus frequency at a moment in time."""
    freqs_hz: np.ndarray
    power_dbm: np.ndarray
    timestamp: float


@dataclass
class Baseline:
    """A learned clean RF profile: per-bin mean and standard deviation."""
    freqs_hz: np.ndarray
    mean_dbm: np.ndarray
    std_dbm: np.ndarray
    sweep_count: int
    created_at: float


@dataclass
class Candidate:
    """A peak worth investigating, found by the diff detector."""
    center_freq_hz: float
    bandwidth_hz: float
    peak_power_dbm: float
    snr_over_baseline_db: float


@dataclass
class DetectorResult:
    """One detector's signed contribution to the suspicion score."""
    name: str
    contribution: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """A scored, ranked candidate with human-readable reasons."""
    candidate: Candidate
    score: float
    band_label: str
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spectrum.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/spectrum.py tests/test_spectrum.py
git commit -m "feat: add engine data types"
```

---

### Task 3: Band catalog

**Files:**
- Create: `quietroom/bands.py`
- Test: `tests/test_bands.py`

Catalog holds expected/licensed bands. Membership means "explained" (lowers suspicion); absence means "unexplained" (raises it). Covert-transmitter bands are handled separately in the signatures detector, not here.

- [ ] **Step 1: Write the failing test**

`tests/test_bands.py`:
```python
from quietroom.bands import Band, BANDS, label_for


def test_fm_broadcast_is_catalogued():
    band = label_for(98_500_000.0)
    assert band is not None
    assert "FM" in band.label


def test_wifi_24_is_catalogued():
    band = label_for(2_437_000_000.0)
    assert band is not None
    assert "2.4" in band.label or "WiFi" in band.label


def test_unallocated_gap_returns_none():
    # 380 MHz is deliberately left out of the catalog for tests.
    assert label_for(380_000_000.0) is None


def test_band_edges_are_inclusive():
    # Pick the FM band and probe its exact edges.
    fm = next(b for b in BANDS if "FM" in b.label)
    assert label_for(fm.start_hz) is not None
    assert label_for(fm.stop_hz) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bands.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.bands'`)

- [ ] **Step 3: Write the implementation**

`quietroom/bands.py`:
```python
"""Catalog of expected/licensed RF bands for known-emitter subtraction."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    start_hz: float
    stop_hz: float
    label: str


# Expected/licensed bands. Membership means a signal is "explained".
# Deliberately omits 380 MHz so tests have a known unallocated gap.
BANDS: list[Band] = [
    Band(530e3, 1_700e3, "AM broadcast"),
    Band(88e6, 108e6, "FM broadcast"),
    Band(118e6, 137e6, "Airband"),
    Band(174e6, 216e6, "VHF TV / DAB"),
    Band(470e6, 698e6, "UHF TV"),
    Band(824e6, 894e6, "Cellular 850"),
    Band(902e6, 928e6, "ISM 915"),
    Band(1_710e6, 1_780e6, "Cellular 1700"),
    Band(1_850e6, 1_990e6, "Cellular 1900"),
    Band(2_400e6, 2_483.5e6, "WiFi/BLE 2.4 GHz ISM"),
    Band(5_150e6, 5_850e6, "WiFi 5 GHz"),
]


def label_for(freq_hz: float, bands: list[Band] = BANDS) -> Band | None:
    """Return the catalogued band containing freq_hz, or None."""
    for band in bands:
        if band.start_hz <= freq_hz <= band.stop_hz:
            return band
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bands.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/bands.py tests/test_bands.py
git commit -m "feat: add expected-band catalog"
```

---

### Task 4: Baseline builder and comparison

**Files:**
- Create: `quietroom/engine/baseline.py`
- Test: `tests/test_baseline.py`

- [ ] **Step 1: Write the failing test**

`tests/test_baseline.py`:
```python
import numpy as np
import pytest

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.engine.baseline import build_baseline, zscores


def _spectrum(power, t=0.0):
    return PowerSpectrum(
        freqs_hz=np.array([100e6, 101e6, 102e6]),
        power_dbm=np.array(power, dtype=float),
        timestamp=t,
    )


def test_build_baseline_computes_mean_and_std():
    base = build_baseline([
        _spectrum([-90, -80, -85], t=1.0),
        _spectrum([-92, -78, -85], t=2.0),
    ])
    assert base.sweep_count == 2
    np.testing.assert_allclose(base.mean_dbm, [-91, -79, -85])
    assert base.created_at == 2.0
    assert base.std_dbm[2] == 0.0


def test_build_baseline_rejects_mismatched_bins():
    a = _spectrum([-90, -90, -90])
    b = PowerSpectrum(
        freqs_hz=np.array([1.0, 2.0]),
        power_dbm=np.array([-90.0, -90.0]),
        timestamp=0.0,
    )
    with pytest.raises(ValueError):
        build_baseline([a, b])


def test_build_baseline_rejects_empty():
    with pytest.raises(ValueError):
        build_baseline([])


def test_zscores_flag_excess_power():
    base = build_baseline([
        _spectrum([-90, -90, -90]),
        _spectrum([-90, -90, -90]),
    ])
    live = _spectrum([-90, -50, -90])  # big jump in bin 1
    z = zscores(live, base)
    # std is floored, so bin 1 should show a large positive z and others ~0.
    assert z[1] > z[0]
    assert z[1] > z[2]
    assert z[0] == pytest.approx(0.0, abs=1e-9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_baseline.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.baseline'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/baseline.py`:
```python
"""Build a clean RF baseline and compare a live sweep against it."""
from __future__ import annotations

import numpy as np

from quietroom.engine.spectrum import Baseline, PowerSpectrum

# Floor on per-bin std (dB) so dead-quiet bins do not produce huge z-scores
# from a tiny denominator.
STD_FLOOR_DB = 2.0


def build_baseline(spectra: list[PowerSpectrum]) -> Baseline:
    """Average several sweeps into a per-bin mean and standard deviation."""
    if not spectra:
        raise ValueError("need at least one spectrum to build a baseline")
    freqs = spectra[0].freqs_hz
    for s in spectra:
        if not np.array_equal(s.freqs_hz, freqs):
            raise ValueError("all spectra must share the same frequency bins")
    stack = np.vstack([s.power_dbm for s in spectra])
    return Baseline(
        freqs_hz=freqs,
        mean_dbm=stack.mean(axis=0),
        std_dbm=stack.std(axis=0),
        sweep_count=len(spectra),
        created_at=spectra[-1].timestamp,
    )


def zscores(live: PowerSpectrum, baseline: Baseline) -> np.ndarray:
    """Per-bin standard-score of live power above the baseline mean."""
    if not np.array_equal(live.freqs_hz, baseline.freqs_hz):
        raise ValueError("live and baseline must share frequency bins")
    std = np.maximum(baseline.std_dbm, STD_FLOOR_DB)
    return (live.power_dbm - baseline.mean_dbm) / std
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_baseline.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/baseline.py tests/test_baseline.py
git commit -m "feat: add baseline builder and z-score comparison"
```

---

### Task 5: Diff detector (candidate finder)

**Files:**
- Create: `quietroom/engine/detectors/diff.py`
- Test: `tests/test_diff.py`

- [ ] **Step 1: Write the failing test**

`tests/test_diff.py`:
```python
import numpy as np

from quietroom.engine.spectrum import PowerSpectrum
from quietroom.engine.baseline import build_baseline
from quietroom.engine.detectors.diff import find_candidates


def _flat(power_dbm, t=0.0):
    freqs = np.arange(100e6, 100e6 + 10e3 * 10, 10e3)  # 10 bins, 10 kHz apart
    return PowerSpectrum(
        freqs_hz=freqs,
        power_dbm=np.full(10, power_dbm, dtype=float),
        timestamp=t,
    )


def test_identical_to_baseline_yields_no_candidates():
    base = build_baseline([_flat(-90), _flat(-90), _flat(-90)])
    live = _flat(-90)
    assert find_candidates(live, base) == []


def test_planted_carrier_becomes_one_candidate():
    base = build_baseline([_flat(-90), _flat(-90), _flat(-90)])
    live = _flat(-90)
    live.power_dbm[4] = -40.0  # one strong bin
    cands = find_candidates(live, base, k=4.0)
    assert len(cands) == 1
    c = cands[0]
    assert c.center_freq_hz == live.freqs_hz[4]
    assert c.peak_power_dbm == -40.0
    assert c.snr_over_baseline_db > 40.0


def test_two_separated_carriers_become_two_candidates():
    base = build_baseline([_flat(-90), _flat(-90), _flat(-90)])
    live = _flat(-90)
    live.power_dbm[2] = -50.0
    live.power_dbm[7] = -55.0
    cands = find_candidates(live, base, k=4.0)
    assert len(cands) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diff.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.detectors.diff'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/detectors/diff.py`:
```python
"""Baseline-differencing detector: turn excess-power bins into candidates."""
from __future__ import annotations

import numpy as np

from quietroom.engine.baseline import zscores
from quietroom.engine.spectrum import Baseline, Candidate, PowerSpectrum


def find_candidates(
    live: PowerSpectrum,
    baseline: Baseline,
    k: float = 4.0,
    min_bins: int = 1,
) -> list[Candidate]:
    """Group contiguous bins whose power exceeds baseline by > k sigma."""
    z = zscores(live, baseline)
    mask = z > k
    n = len(mask)
    bin_hz = float(live.freqs_hz[1] - live.freqs_hz[0]) if n > 1 else 0.0
    candidates: list[Candidate] = []

    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        if (j - i) >= min_bins:
            seg_freqs = live.freqs_hz[i:j]
            seg_power = live.power_dbm[i:j]
            peak = int(np.argmax(seg_power))
            bw = float(seg_freqs[-1] - seg_freqs[0]) or bin_hz
            excess = float(seg_power[peak] - baseline.mean_dbm[i + peak])
            candidates.append(
                Candidate(
                    center_freq_hz=float(seg_freqs[peak]),
                    bandwidth_hz=bw,
                    peak_power_dbm=float(seg_power[peak]),
                    snr_over_baseline_db=excess,
                )
            )
        i = j
    return candidates
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diff.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/detectors/diff.py tests/test_diff.py
git commit -m "feat: add baseline-diff candidate finder"
```

---

### Task 6: Catalog detector

**Files:**
- Create: `quietroom/engine/detectors/catalog.py`
- Test: `tests/test_catalog.py`

- [ ] **Step 1: Write the failing test**

`tests/test_catalog.py`:
```python
from quietroom.engine.spectrum import Candidate
from quietroom.engine.detectors.catalog import score_band


def _cand(freq):
    return Candidate(center_freq_hz=freq, bandwidth_hz=20e3,
                     peak_power_dbm=-50.0, snr_over_baseline_db=20.0)


def test_known_band_lowers_suspicion():
    r = score_band(_cand(98_500_000.0))  # FM broadcast
    assert r.name == "catalog"
    assert r.contribution < 0
    assert "FM" in r.reasons[0]


def test_unknown_band_raises_suspicion():
    r = score_band(_cand(380_000_000.0))  # deliberate catalog gap
    assert r.contribution > 0
    assert "not in any known band" in r.reasons
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.detectors.catalog'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/detectors/catalog.py`:
```python
"""Known-emitter subtraction: explained bands lower suspicion."""
from __future__ import annotations

from quietroom.bands import Band, BANDS, label_for
from quietroom.engine.spectrum import Candidate, DetectorResult

UNKNOWN_BAND_POINTS = 30.0
KNOWN_BAND_POINTS = -40.0


def score_band(candidate: Candidate, bands: list[Band] = BANDS) -> DetectorResult:
    band = label_for(candidate.center_freq_hz, bands)
    if band is None:
        return DetectorResult("catalog", UNKNOWN_BAND_POINTS,
                              ["not in any known band"])
    return DetectorResult("catalog", KNOWN_BAND_POINTS,
                          [f"in known {band.label} band"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/detectors/catalog.py tests/test_catalog.py
git commit -m "feat: add catalog (known-band) detector"
```

---

### Task 7: Signatures detector

**Files:**
- Create: `quietroom/engine/detectors/signatures.py`
- Test: `tests/test_signatures.py`

- [ ] **Step 1: Write the failing test**

`tests/test_signatures.py`:
```python
from quietroom.engine.spectrum import Candidate
from quietroom.engine.detectors.signatures import score_signatures


def _cand(freq=200e6, bw=20e3, power=-70.0):
    return Candidate(center_freq_hz=freq, bandwidth_hz=bw,
                     peak_power_dbm=power, snr_over_baseline_db=20.0)


def test_narrowband_flag():
    r = score_signatures(_cand(bw=10e3))
    assert "narrowband carrier" in r.reasons
    assert r.contribution > 0


def test_wideband_is_not_narrowband():
    r = score_signatures(_cand(bw=5_000_000.0, power=-95.0))
    assert "narrowband carrier" not in r.reasons


def test_near_field_strong_flag():
    r = score_signatures(_cand(power=-30.0))
    assert "near-field strong signal" in r.reasons


def test_covert_band_flag():
    r = score_signatures(_cand(freq=380e6))  # inside 300-470 MHz covert band
    assert any("analog bug band" in reason for reason in r.reasons)


def test_clean_candidate_scores_zero():
    r = score_signatures(_cand(freq=200e6, bw=5_000_000.0, power=-95.0))
    assert r.contribution == 0.0
    assert r.reasons == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_signatures.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.detectors.signatures'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/detectors/signatures.py`:
```python
"""Bug-signature heuristics: telltale shapes and bands of covert transmitters."""
from __future__ import annotations

from quietroom.engine.spectrum import Candidate, DetectorResult

NARROWBAND_HZ = 50_000.0
NEARFIELD_DBM = -40.0
NARROWBAND_POINTS = 15.0
NEARFIELD_POINTS = 15.0
COVERT_POINTS = 20.0

# (start_hz, stop_hz, label) bands disproportionately used by covert transmitters.
COVERT_BANDS = [
    (300e6, 470e6, "common analog bug band"),
    (1_200e6, 1_300e6, "1.2 GHz analog video band"),
]


def score_signatures(candidate: Candidate) -> DetectorResult:
    contribution = 0.0
    reasons: list[str] = []

    if candidate.bandwidth_hz <= NARROWBAND_HZ:
        contribution += NARROWBAND_POINTS
        reasons.append("narrowband carrier")

    if candidate.peak_power_dbm >= NEARFIELD_DBM:
        contribution += NEARFIELD_POINTS
        reasons.append("near-field strong signal")

    for start, stop, label in COVERT_BANDS:
        if start <= candidate.center_freq_hz <= stop:
            contribution += COVERT_POINTS
            reasons.append(f"in {label}")
            break

    return DetectorResult("signatures", contribution, reasons)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_signatures.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/detectors/signatures.py tests/test_signatures.py
git commit -m "feat: add bug-signature heuristics detector"
```

---

### Task 8: Audio-correlation detector

**Files:**
- Create: `quietroom/engine/detectors/audio_corr.py`
- Test: `tests/test_audio_corr.py`

This is the pure math: given a candidate's demodulated RF envelope and a
time-aligned reference audio buffer (same length, same sample rate), return a
0-to-1 correlation. Tuning, demodulation, and resampling/alignment live in the
radio and audio layers (next plan); here we assume aligned arrays.

- [ ] **Step 1: Write the failing test**

`tests/test_audio_corr.py`:
```python
import numpy as np

from quietroom.engine.detectors.audio_corr import (
    envelope_correlation, score_audio,
)


def test_correlated_envelope_scores_high():
    t = np.linspace(0, 1, 500)
    audio = np.sin(2 * np.pi * 5 * t)
    rf_envelope = 3.0 * audio + 0.01 * np.random.default_rng(0).normal(size=t.size)
    corr = envelope_correlation(rf_envelope, audio)
    assert corr > 0.9


def test_uncorrelated_envelope_scores_low():
    rng = np.random.default_rng(1)
    audio = rng.normal(size=500)
    rf_envelope = rng.normal(size=500)
    corr = envelope_correlation(rf_envelope, audio)
    assert corr < 0.3


def test_score_audio_fires_above_threshold():
    r = score_audio(0.82)
    assert r.name == "audio"
    assert r.contribution > 0
    assert "0.82" in r.reasons[0]


def test_score_audio_silent_below_threshold():
    r = score_audio(0.1)
    assert r.contribution == 0.0
    assert r.reasons == []


def test_empty_inputs_return_zero():
    assert envelope_correlation(np.array([]), np.array([])) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_audio_corr.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.detectors.audio_corr'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/detectors/audio_corr.py`:
```python
"""Audio-correlation detector: does an RF envelope move with room audio?"""
from __future__ import annotations

import numpy as np

from quietroom.engine.spectrum import DetectorResult

AUDIO_CORR_THRESHOLD = 0.5
AUDIO_POINTS = 50.0


def envelope_correlation(rf_envelope: np.ndarray, audio_ref: np.ndarray) -> float:
    """Normalized cross-correlation magnitude (0..1) of two aligned signals."""
    a = np.asarray(rf_envelope, dtype=float)
    b = np.asarray(audio_ref, dtype=float)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    denom = np.sqrt(np.sum(a * a) * np.sum(b * b))
    if denom == 0:
        return 0.0
    return float(abs(np.dot(a, b)) / denom)


def score_audio(correlation: float) -> DetectorResult:
    if correlation >= AUDIO_CORR_THRESHOLD:
        return DetectorResult(
            "audio", AUDIO_POINTS,
            [f"envelope correlates with room audio at {correlation:.2f}"],
        )
    return DetectorResult("audio", 0.0, [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_audio_corr.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/detectors/audio_corr.py tests/test_audio_corr.py
git commit -m "feat: add audio-correlation detector"
```

---

### Task 9: Score fusion

**Files:**
- Create: `quietroom/engine/score.py`
- Test: `tests/test_score.py`

- [ ] **Step 1: Write the failing test**

`tests/test_score.py`:
```python
from quietroom.engine.spectrum import Candidate, DetectorResult
from quietroom.engine.score import score_candidate


def _cand(freq=380e6, snr=18.0):
    return Candidate(center_freq_hz=freq, bandwidth_hz=10e3,
                     peak_power_dbm=-35.0, snr_over_baseline_db=snr)


def test_score_sums_contributions_and_clamps():
    results = [
        DetectorResult("catalog", 30.0, ["not in any known band"]),
        DetectorResult("signatures", 35.0, ["narrowband carrier",
                                            "near-field strong signal"]),
        DetectorResult("audio", 50.0, ["envelope correlates with room audio at 0.82"]),
    ]
    f = score_candidate(_cand(), results)
    assert f.score == 100.0  # clamped
    assert "narrowband carrier" in f.reasons
    assert f.breakdown["audio"] == 50.0
    assert "snr" in f.breakdown


def test_known_band_can_pull_score_down():
    results = [
        DetectorResult("catalog", -40.0, ["in known FM broadcast band"]),
        DetectorResult("signatures", 0.0, []),
        DetectorResult("audio", 0.0, []),
    ]
    f = score_candidate(_cand(freq=98_500_000.0, snr=10.0), results)
    assert f.score < 20.0
    assert f.band_label == "FM broadcast"


def test_band_label_unknown_when_uncatalogued():
    f = score_candidate(_cand(freq=380e6), [])
    assert f.band_label == "unknown"


def test_score_never_negative():
    results = [DetectorResult("catalog", -40.0, [])]
    f = score_candidate(_cand(freq=98_500_000.0, snr=0.0), results)
    assert f.score >= 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_score.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.score'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/score.py`:
```python
"""Fuse detector outputs into a 0-100 suspicion score with reasons."""
from __future__ import annotations

from quietroom.bands import label_for
from quietroom.engine.spectrum import Candidate, DetectorResult, Finding

# Raw SNR over baseline contributes a small, capped amount on its own.
SNR_CAP_DB = 20.0


def score_candidate(
    candidate: Candidate,
    results: list[DetectorResult],
) -> Finding:
    breakdown: dict[str, float] = {}
    reasons: list[str] = []

    snr_term = min(max(candidate.snr_over_baseline_db, 0.0), SNR_CAP_DB)
    total = snr_term
    breakdown["snr"] = snr_term

    for r in results:
        total += r.contribution
        breakdown[r.name] = r.contribution
        reasons.extend(r.reasons)

    score = float(max(0.0, min(100.0, total)))
    band = label_for(candidate.center_freq_hz)
    band_label = band.label if band else "unknown"

    return Finding(
        candidate=candidate,
        score=score,
        band_label=band_label,
        reasons=reasons,
        breakdown=breakdown,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_score.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add quietroom/engine/score.py tests/test_score.py
git commit -m "feat: add suspicion-score fusion"
```

---

### Task 10: End-to-end pipeline test

**Files:**
- Create: `quietroom/engine/pipeline.py`
- Test: `tests/test_pipeline.py`

A thin orchestrator that wires the engine together: baseline + live sweep in,
ranked findings out. This is the function the radio/web layer will call.

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'quietroom.engine.pipeline'`)

- [ ] **Step 3: Write the implementation**

`quietroom/engine/pipeline.py`:
```python
"""Orchestrate the engine: baseline + live sweep -> ranked findings."""
from __future__ import annotations

from quietroom.engine.detectors.catalog import score_band
from quietroom.engine.detectors.diff import find_candidates
from quietroom.engine.detectors.signatures import score_signatures
from quietroom.engine.score import score_candidate
from quietroom.engine.spectrum import Baseline, Finding, PowerSpectrum


def sweep_findings(
    live: PowerSpectrum,
    baseline: Baseline,
    k: float = 4.0,
) -> list[Finding]:
    """Run the diff + catalog + signature detectors and rank the results.

    The audio-correlation detector is intentionally not run here: it requires
    tuning and microphone capture and is triggered on demand per candidate by
    the radio/web layer, which then re-scores that candidate with an extra
    audio DetectorResult.
    """
    findings: list[Finding] = []
    for candidate in find_candidates(live, baseline, k=k):
        results = [score_band(candidate), score_signatures(candidate)]
        findings.append(score_candidate(candidate, results))
    findings.sort(key=lambda f: f.score, reverse=True)
    return findings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole suite and commit**

Run: `uv run pytest -v`
Expected: PASS (all tests, ~30 across the engine)

```bash
git add quietroom/engine/pipeline.py tests/test_pipeline.py
git commit -m "feat: add engine pipeline orchestrator"
```

---

## Self-Review

**Spec coverage (engine portion of spec sections 4-7, 12):**
- Data types (spec 5): Task 2. Covered.
- Baseline build/compare (spec 6.1-6.2): Task 4. Covered.
- Diff detector (spec 4 spine, 6.2): Task 5. Covered.
- Catalog / known-emitter subtraction (spec 6.3): Tasks 3 + 6. Covered.
- Signature heuristics (spec 6.3): Task 7. Covered.
- Audio-correlation math (spec 6.3): Task 8. The tuning/mic capture half is explicitly deferred to the radio/audio plan, noted in Task 8 and pipeline docstring.
- Score fusion + weights config (spec 7): Task 9. Weight constants live in each detector module plus `SNR_CAP_DB`; calibration is centralizable later.
- Testing strategy fixtures (spec 12): Tasks 5, 9, 10 implement the baseline-identical (no findings), planted-narrowband-in-unknown-band (high score), and known-band (low score) fixtures.
- Radio layer, audio capture, web UI, demo mode (spec 8-11): NOT in this plan by design; they are the next plan.

**Placeholder scan:** No TBD/TODO/"handle edge cases" placeholders; every code step is complete.

**Type consistency:** `DetectorResult(name, contribution, reasons)`, `Candidate(center_freq_hz, bandwidth_hz, peak_power_dbm, snr_over_baseline_db)`, and `Finding(candidate, score, band_label, reasons, breakdown)` are used identically across Tasks 2, 5, 6, 7, 8, 9, 10. `label_for(freq_hz, bands=BANDS)` signature is consistent between Tasks 3, 6, 9. `find_candidates(live, baseline, k, min_bins)` consistent between Tasks 5 and 10.

**Note for next plan (radio + audio + web):** will add `radio/device.py` (with a recorded/fake device for demo mode), `radio/hackrf.py`, `audio/capture.py`, the on-demand audio re-scoring path that appends a `score_audio()` result to a candidate, the Flask + Socket.IO UI, and SigMF fixtures.
