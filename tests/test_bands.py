import pytest

from quietroom.bands import BANDS, label_for


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



@pytest.mark.parametrize("freq,needle", [
    (740e6, "700"),          # LTE 700
    (2_150e6, "AWS"),        # AWS downlink
    (2_600e6, "2.5"),        # BRS/EBS
    (3_600e6, "CBRS"),       # CBRS
    (433_920_000.0, "433"),  # ISM 433 (garage remotes, sensors)
])
def test_common_us_allocations_are_catalogued(freq, needle):
    band = label_for(freq)
    assert band is not None and needle in band.label


def test_eu_region_has_868_ism_and_us_does_not():
    from quietroom.bands import bands_for
    us = label_for(868.3e6, bands_for("us"))
    assert us is None or "868" not in us.label
    eu = label_for(868.3e6, bands_for("eu"))
    assert eu is not None and "868" in eu.label


def test_unknown_region_raises():
    from quietroom.bands import bands_for
    with pytest.raises(ValueError):
        bands_for("mars")


def test_bands_have_a_kind():
    assert {b.kind for b in BANDS} <= {"broadcast", "shared"}
