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
