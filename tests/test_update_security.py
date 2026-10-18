import hashlib
import pytest
from quietroom.update import sign_manifest, verify_manifest, StagedUpdater, check_secret_file


def _artifact(tmp_path, data=b"wheel-bytes"):
    p = tmp_path / "q.whl"
    p.write_bytes(data)
    m = {"version": "0.4.0", "artifact_sha256": hashlib.sha256(data).hexdigest()}
    m["signature"] = sign_manifest(m, "key1234567890123")
    return p, m


def test_verify_ok_and_tamper_fails(tmp_path):
    p, m = _artifact(tmp_path)
    assert verify_manifest(p, m, "key1234567890123")["version"] == "0.4.0"
    p.write_bytes(b"evil")
    with pytest.raises(ValueError):
        verify_manifest(p, m, "key1234567890123")
    p, m = _artifact(tmp_path)
    with pytest.raises(ValueError):
        verify_manifest(p, m, "wrong-key-000000")


def test_staged_update_and_rollback(tmp_path):
    p, m = _artifact(tmp_path, b"v1")
    u = StagedUpdater(tmp_path / "rel")
    u.stage_and_activate(p, m, "key1234567890123")
    assert u.current_version() == "0.4.0"
    p2, m2 = _artifact(tmp_path, b"v2")
    m2["version"] = "0.5.0"
    m2["signature"] = sign_manifest(m2, "key1234567890123")
    u.stage_and_activate(p2, m2, "key1234567890123")
    assert u.current_version() == "0.5.0"
    u.rollback()
    assert u.current_version() == "0.4.0"


def test_secret_file_permissions(tmp_path):
    f = tmp_path / "token"
    f.write_text("x")
    f.chmod(0o644)
    assert "world-readable" in " ".join(check_secret_file(f))
    f.chmod(0o600)
    assert check_secret_file(f) == []
