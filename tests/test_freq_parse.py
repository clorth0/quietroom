import argparse

import pytest

from quietroom.cli import parse_freq


@pytest.mark.parametrize("text,hz", [
    ("308M", 308_000_000),
    ("308.5MHz", 308_500_000),
    ("2.4G", 2_400_000_000),
    ("2.4 GHz", 2_400_000_000),
    ("25k", 25_000),
    ("433.92m", 433_920_000),
    ("300000000", 300_000_000),     # bare numbers stay Hz (backward compatible)
])
def test_parse_freq(text, hz):
    assert parse_freq(text) == hz


@pytest.mark.parametrize("bad", ["", "abc", "-5M", "3X"])
def test_parse_freq_rejects_garbage(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_freq(bad)
