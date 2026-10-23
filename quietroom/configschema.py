"""Declarative validation for the node's TOML config.

Every section of the file is described by a table of :class:`Field` specs:
the type, the allowed range or choices, and the default. :func:`check_section`
applies one table to one TOML table and records a :class:`ConfigIssue` per
problem (unknown key with a did-you-mean, wrong type, out of range, secret in
the file) instead of stopping at the first, so an operator sees everything
wrong with a config in one go. ``quietroom.config`` builds its dataclasses
from the checked values and adds the few cross-field rules.
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

_FREQ_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:hz)?\s*$", re.I)
_SCALE = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}

# Secrets are read from the service environment only; the loader refuses them
# in TOML so they never land in a world-readable file, a backup or a diff.
SECRET_ENV = {
    "alerts.webhook_secret": "QUIETROOM_WEBHOOK_SECRET",
    "alerts.email.password": "QUIETROOM_SMTP_PASSWORD",
    "fleet.token": "QUIETROOM_CENTRAL_TOKEN",
}


@dataclass(frozen=True)
class ConfigIssue:
    """One problem: the dotted key it is about and what is wrong / allowed."""

    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.key}: {self.message}" if self.key else self.message


class ConfigError(ValueError):
    """A config that must not be run. Lists every problem found, one per line."""

    def __init__(self, issues: list[ConfigIssue], source: str = ""):
        self.issues = list(issues)
        self.source = source
        where = f" in {source}" if source else ""
        lines = "".join(f"\n  - {i}" for i in self.issues)
        super().__init__(f"invalid config{where}:{lines}")

    def with_source(self, source: str) -> ConfigError:
        return ConfigError(self.issues, source)


Kind = Literal["str", "int", "float", "bool", "freq", "url", "addresses"]
MISSING: Any = object()


def _fmt(x: float) -> str:
    return f"{x:g}"


@dataclass(frozen=True)
class Field:
    """Spec for one key.

    lo/hi bound numbers (``lo_open``/``hi_open`` make a bound exclusive);
    ``also`` lists extra allowed values outside the range (``drift_window``
    is 0 = off, or >= 2). ``choices`` restricts strings, ``schemes`` URLs.
    """

    kind: Kind
    default: Any = MISSING
    lo: float | None = None
    hi: float | None = None
    lo_open: bool = False
    hi_open: bool = False
    also: tuple[float, ...] = ()
    choices: tuple[str, ...] = ()
    schemes: tuple[str, ...] = ()
    nonempty: bool = False
    lower: bool = False
    note: str = ""  # appended to the allowed-values text, e.g. "(0 = never)"

    @property
    def required(self) -> bool:
        return self.default is MISSING

    def describe(self, name: str) -> str:
        """The allowed values, phrased as in the error message."""
        if self.kind in ("int", "float"):
            noun = "an integer" if self.kind == "int" else "a number"
            parts = []
            if self.lo is not None:
                parts.append(f"{_fmt(self.lo)} {'<' if self.lo_open else '<='} ")
            parts.append(name)
            if self.hi is not None:
                parts.append(f" {'<' if self.hi_open else '<='} {_fmt(self.hi)}")
            if self.lo is not None and self.hi is None:
                parts = [name, f" {'>' if self.lo_open else '>='} {_fmt(self.lo)}"]
            text = noun if len(parts) == 1 else f"{noun} with {''.join(parts)}"
            if self.also:
                text += " or " + " or ".join(_fmt(a) for a in self.also)
        elif self.kind == "bool":
            text = "true or false (a TOML boolean, not a string)"
        elif self.choices:
            text = "one of " + ", ".join(repr(c) for c in sorted(self.choices))
        elif self.kind == "freq":
            text = "a frequency in Hz (integer) or a string like '433.92M', '2.4G', '25k'"
        elif self.kind == "url":
            schemes = " or ".join(f"{s}://" for s in self.schemes)
            text = f"empty (off) or an {schemes} URL with a host and no credentials"
        elif self.kind == "addresses":
            text = "an email address or a list of them"
        else:
            text = "a non-empty string" if self.nonempty else "a string"
        return f"{text} {self.note}".rstrip()


def parse_freq(v: Any) -> int:
    if isinstance(v, bool):
        raise ValueError(f"invalid frequency {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v):
        return int(round(v))
    m = _FREQ_RE.match(str(v))
    if not m:
        raise ValueError(f"invalid frequency {v!r}")
    return int(round(float(m.group(1)) * _SCALE[m.group(2).lower()]))


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _in_range(f: Field, v: float) -> bool:
    if v in f.also:
        return True
    if f.lo is not None and (v <= f.lo if f.lo_open else v < f.lo):
        return False
    if f.hi is not None and (v >= f.hi if f.hi_open else v > f.hi):
        return False
    return True


def url_problem(url: str, schemes: tuple[str, ...]) -> str:
    """Why ``url`` is not acceptable ('' if it is). Never echoes the URL itself,
    which may carry a credential."""
    if url != url.strip() or any(c.isspace() for c in url):
        return "contains whitespace"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "is not a valid URL"
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        got = f"scheme {scheme!r}" if scheme else "no scheme"
        return f"has {got}"
    if not parts.hostname:
        return "has no host"
    if parts.username is not None or parts.password is not None:
        return "embeds credentials (user:password@); keep secrets in the environment"
    return ""


def check_value(key: str, f: Field, v: Any, issues: list[ConfigIssue]) -> Any:
    """Validate (and normalise) one value; MISSING after recording an issue."""
    name = key.rsplit(".", 1)[-1]
    allowed = f.describe(name)

    def bad(detail: str = "") -> Any:
        issues.append(ConfigIssue(key, f"must be {allowed}{detail}"))
        return MISSING

    if f.kind in ("int", "float"):
        ok_type = isinstance(v, int) and not isinstance(v, bool) if f.kind == "int" else (
            _is_number(v)
        )
        if not ok_type or not _in_range(f, v):
            return bad(f", got {v!r}")
        return v if f.kind == "int" else float(v)
    if f.kind == "bool":
        return v if isinstance(v, bool) else bad(f", got {v!r}")
    if f.kind == "freq":
        try:
            return parse_freq(v)
        except ValueError:
            return bad(f", got {v!r}")
    if f.kind == "addresses":
        items = [v] if isinstance(v, str) else v
        if not isinstance(items, list) or not items or not all(isinstance(a, str) for a in items):
            return bad()
        return items
    if not isinstance(v, str):
        return bad(f", got {type(v).__name__}")
    if f.kind == "url":
        if v == "":
            return v
        problem = url_problem(v, f.schemes)
        return bad(f"; this one {problem}") if problem else v
    norm = v.lower() if f.lower else v
    if f.choices and norm not in f.choices:
        return bad(f", got {v!r}")
    if f.nonempty and not v.strip():
        return bad()
    return norm


def unknown_key_issue(key: str, name: str, allowed: list[str]) -> ConfigIssue:
    close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
    hint = f"; did you mean {close[0]!r}?" if close else ""
    return ConfigIssue(
        key, f"unknown key {key!r}{hint} (allowed: {', '.join(sorted(allowed))})"
    )


def secret_issue(key: str) -> ConfigIssue:
    env = SECRET_ENV[key]
    return ConfigIssue(
        key,
        f"secrets must not be stored in the config file; remove {key.rsplit('.', 1)[-1]} "
        f"and set {env} in the service environment (systemd EnvironmentFile)",
    )


def join(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def check_section(
    prefix: str,
    raw: Any,
    spec: Mapping[str, Field],
    issues: list[ConfigIssue],
    *,
    subtables: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Apply ``spec`` to the TOML table ``raw``.

    Returns the valid values with defaults filled in; keys that failed are
    left out (an issue was recorded) so callers can carry on collecting.
    ``subtables`` are keys handled by the caller (e.g. ``alerts.email``).
    """
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(prefix, f"must be a table ([{prefix}]), got {raw!r}"))
        return {k: f.default for k, f in spec.items() if not f.required}
    out: dict[str, Any] = {}
    allowed = sorted([*spec, *subtables])
    for k, v in raw.items():
        key = join(prefix, k)
        if key in SECRET_ENV:
            issues.append(secret_issue(key))
        elif k in subtables:
            continue
        elif k not in spec:
            issues.append(unknown_key_issue(key, k, allowed))
        else:
            got = check_value(key, spec[k], v, issues)
            if got is not MISSING:
                out[k] = got
    for k, f in spec.items():
        if k in raw:
            continue
        if f.required:
            issues.append(ConfigIssue(join(prefix, k), f"is required ({f.describe(k)})"))
        else:
            out[k] = f.default
    return out
