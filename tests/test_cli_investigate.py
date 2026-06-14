from quietroom.cli import format_verdict, main


def test_format_verdict_live_bug():
    out = format_verdict(308_000_000.0, 0.83)
    assert "308.000" in out
    assert "0.83" in out
    assert "LIKELY LIVE BUG" in out


def test_format_verdict_clean():
    out = format_verdict(308_000_000.0, 0.05)
    assert "no audio correlation" in out.lower()


def test_main_demo_investigate(capsys):
    # In --demo, the audio test is synthetic and returns a high correlation.
    rc = main(["--demo", "--investigate", "308000000"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "308.000" in captured.out
    assert "LIKELY LIVE BUG" in captured.out
