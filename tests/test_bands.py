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
