import json
import os
import pytest

def test_simulated_soak_completes_and_reports(tmp_path):
    from tools.soak import run_soak
    r=run_soak(ticks=5, out=tmp_path/"soak.json")
    assert r["ticks"]==5 and r["errors"]==0 and r["findings"]>=1
    assert json.loads((tmp_path/"soak.json").read_text())["ticks"]==5

@pytest.mark.skipif(os.environ.get("QUIETROOM_HW")!="1", reason="no SDR hardware attached")
def test_hardware_smoke_placeholder():
    from quietroom.radio.hackrf import HackRFDevice
    assert HackRFDevice().probe() is not None
