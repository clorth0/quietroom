"""Signed, staged updates with rollback for deployed nodes.

A release manifest is {version, artifact_sha256, signature} where signature is
HMAC-SHA256 over "version:artifact_sha256" with the node's update key. (HMAC
is stdlib-available on every Pi; asymmetric signing can replace the MAC
without changing the staged/rollback layout.)
Artifacts are staged under releases/<version>/ and activated by swapping a
`current` symlink; `previous` enables one-step rollback. Nothing here runs
the artifact — installation/restart stays with systemd/packaging.
"""

from __future__ import annotations
import hashlib
import hmac
import os
import shutil
from pathlib import Path


def _mac(version, sha, key):
    return hmac.new(key.encode(), f"{version}:{sha}".encode(), hashlib.sha256).hexdigest()


def sign_manifest(manifest, key):
    return _mac(manifest["version"], manifest["artifact_sha256"], key)


def verify_manifest(artifact_path, manifest, key):
    data = Path(artifact_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(sha, manifest.get("artifact_sha256", "")):
        raise ValueError("artifact sha256 mismatch (tampered or corrupt download)")
    expected = _mac(manifest["version"], sha, key)
    if not hmac.compare_digest(expected, manifest.get("signature", "")):
        raise ValueError("manifest signature mismatch (untrusted release)")
    return manifest


class StagedUpdater:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "releases").mkdir(parents=True, exist_ok=True)

    def _link(self, name, target):
        link = self.root / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)

    def current_version(self):
        cur = self.root / "current"
        return cur.resolve().name if cur.exists() else None

    def stage_and_activate(self, artifact_path, manifest, key):
        verify_manifest(artifact_path, manifest, key)
        dest = self.root / "releases" / manifest["version"]
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact_path, dest / Path(artifact_path).name)
        prev = self.current_version()
        if prev and prev != manifest["version"]:
            self._link("previous", f"releases/{prev}")
        self._link("current", f"releases/{manifest['version']}")
        return manifest["version"]

    def rollback(self):
        prev = self.root / "previous"
        if not prev.exists():
            raise ValueError("no previous release to roll back to")
        target = os.readlink(prev)
        self._link("current", target)
        return self.current_version()


def check_secret_file(path):
    """Return hardening warnings for a secret-bearing file (token/key)."""
    p = Path(path)
    warnings = []
    if not p.exists():
        return [f"{p} does not exist"]
    mode = p.stat().st_mode
    if mode & 0o077:
        warnings.append(f"{p} is group/world-readable (mode {oct(mode)}); use 0600")
    if mode & 0o004:
        warnings.append(f"{p} is world-readable")
    return warnings
