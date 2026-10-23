"""Config audit report shared by ``quietroom-node`` and ``quietroom-central --check-config``.

Secret values are never stored here: only whether each one is present.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def present(env: Mapping[str, str], name: str) -> bool:
    """Set and not blank; the value itself is never returned or logged."""
    return bool((env.get(name) or "").strip())


class Report:
    def __init__(self) -> None:
        self.secrets: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def secret(
        self, env: Mapping[str, str], name: str, purpose: str, *, required: bool, missing: str
    ) -> bool:
        ok = present(env, name)
        self.secrets.append({"env": name, "for": purpose, "required": required, "present": ok})
        if not ok:
            (self.errors if required else self.warnings).append(missing)
        return ok

    def check(self, name: str, ok: bool, problem: str = "") -> bool:
        self.checks.append({"check": name, "ok": ok, **({"problem": problem} if problem else {})})
        if not ok:
            self.errors.append(problem or name)
        return ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "errors": self.errors,
            "warnings": self.warnings,
            "secrets": self.secrets,
            "checks": self.checks,
        }
