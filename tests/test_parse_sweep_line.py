from quietroom.radio.parse import parse_sweep_line


def test_parses_hackrf_sweep_csv_line():
    # date, time, hz_low, hz_high, bin_width, num_samples, then powers (dBm)
    line = "2026-06-14, 15:00:00.000, 100000000, 105000000, 1000000.00, 20, -80.5, -79.1, -95.0"
    hz_low, bin_width, powers = parse_sweep_line(line)
    assert hz_low == 100_000_000
    assert bin_width == 1_000_000.0
    assert powers == [-80.5, -79.1, -95.0]


def test_handles_trailing_whitespace():
    line = "2026-06-14, 15:00:00.000, 200000000, 201000000, 1000000.00, 20, -70.0\n"
    hz_low, bin_width, powers = parse_sweep_line(line)
    assert hz_low == 200_000_000
    assert powers == [-70.0]
