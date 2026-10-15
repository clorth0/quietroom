from quietroom.engine.spectrum import Candidate, Finding
from quietroom.cli import format_findings, main


def test_format_findings_renders_table():
    f = Finding(
        candidate=Candidate(308_000_000.0, 1_000_000.0, -30.0, 65.0),
        score=92.0,
        band_label="unknown",
        reasons=["not in any known band", "narrowband carrier"],
        breakdown={},
    )
    out = format_findings([f])
    assert "308.000" in out          # MHz, 3 decimals
    assert "92" in out
    assert "narrowband carrier" in out


def test_format_findings_clean_room_message():
    out = format_findings([])
    assert "No suspicious" in out


def test_main_demo_prints_a_finding(capsys):
    rc = main(["--demo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "308.000" in captured.out   # the demo planted bug at 308 MHz


def test_demo_flags_unresolved_single_bin():
    import contextlib
    import io
    from quietroom.cli import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["--demo"]) == 0
    assert "unresolved single-bin" in buf.getvalue()


def test_region_flag_accepts_eu():
    import contextlib
    import io
    from quietroom.cli import main
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["--demo", "--region", "eu"]) == 0


def test_live_sweeps_and_min_hits_flags():
    import contextlib
    import io
    from quietroom.cli import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["--demo", "--live-sweeps", "3", "--min-hits", "2"]) == 0
    assert "persistent" in buf.getvalue()


def test_min_hits_cannot_exceed_live_sweeps():
    import pytest
    from quietroom.cli import main
    with pytest.raises(SystemExit):
        main(["--demo", "--live-sweeps", "2", "--min-hits", "3"])


def test_cli_accepts_mhz_and_gain_flags(capsys, monkeypatch):
    rc = main(["--demo", "--start", "300M", "--stop", "320M", "--bin", "1M",
               "-k", "5", "--lna", "24", "--vga", "30"])
    assert rc == 0
    assert "308.000" in capsys.readouterr().out


def test_cli_rejects_invalid_gain():
    import pytest
    with pytest.raises(SystemExit):
        main(["--demo", "--lna", "7"])


def test_verbose_shows_score_breakdown(capsys):
    assert main(["--demo", "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "snr=" in out and "catalog=" in out and "signatures=" in out


def test_k_threshold_is_used(capsys):
    # The demo bug is ~65 dB over a flat baseline (std floor 2 dB => z~32);
    # an absurd k suppresses it, proving -k reaches the detector.
    assert main(["--demo", "-k", "100"]) == 0
    assert "No suspicious" in capsys.readouterr().out


def test_verbose_shows_raw_total(capsys):
    assert main(["--demo", "-v"]) == 0
    assert "(raw " in capsys.readouterr().out


def test_default_bin_resolves_narrowband_carriers(monkeypatch):
    # Default bin must be <= the 50 kHz narrowband threshold so a real bug can
    # earn full narrowband credit without the user knowing to pass --bin.
    import quietroom.cli as cli
    from quietroom.engine.detectors.signatures import NARROWBAND_HZ
    seen = {}

    class FakeRadio:
        def __init__(self, *a, **k):
            pass

        def probe(self):
            return None          # stop right after arg parsing

    monkeypatch.setattr(cli, "HackRFDevice", FakeRadio)
    real_parse = cli.argparse.ArgumentParser.parse_args

    def spy(self, *a, **k):
        ns = real_parse(self, *a, **k)
        seen["bin"] = ns.bin
        return ns

    monkeypatch.setattr(cli.argparse.ArgumentParser, "parse_args", spy)
    assert cli.main([]) == 1
    assert seen["bin"] <= NARROWBAND_HZ


def test_web_session_default_bin_matches_cli():
    from quietroom.radio.recorded import streaming_demo_device
    from quietroom.web.session import ScanSession
    from quietroom.cli import DEFAULT_BIN_HZ
    assert ScanSession(streaming_demo_device()).bin_hz == DEFAULT_BIN_HZ
