"""Who may open the UI. A list cannot stand in for this check."""
from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_REMOTE_HINT = "(prefer an SSH tunnel or Tailscale to 127.0.0.1)"
_TOKEN_REQUIRED = "--allow-remote needs QUIETROOM_TOKEN set (16+ chars)"


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refusing(host: str) -> str:
    return f"refusing to bind {host}: pass --allow-remote {_REMOTE_HINT}"


@dataclass(frozen=True)
class LocalBind:
    """Loopback only. A token is optional, the same as today's localhost server."""

    host: str = "127.0.0.1"
    token: str | None = None

    def __post_init__(self) -> None:
        if not is_loopback(self.host):
            raise ValueError(_refusing(self.host))

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return LOOPBACK_HOSTS


@dataclass(frozen=True)
class RemoteBind:
    """A non-loopback host that already presented a long token."""

    host: str
    token: str

    def __post_init__(self) -> None:
        if len(self.token) < 16:
            raise ValueError(_TOKEN_REQUIRED)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return LOOPBACK_HOSTS | {self.host}


Bind = LocalBind | RemoteBind


def resolve_bind(host: str, *, allow_remote: bool, token: str | None) -> Bind:
    """Return the bind main() is allowed to start, or raise the refusal it prints."""
    if is_loopback(host):
        return LocalBind(host, token)
    if not allow_remote:
        raise ValueError(_refusing(host))
    if not token or len(token) < 16:
        raise ValueError(_TOKEN_REQUIRED)
    return RemoteBind(host, token)


def origin_checker(bind: Bind) -> Callable[..., bool]:
    """Callable for the socket origin check.

    An empty list would disable that check, so this returns a function instead.
    """
    allowed = bind.allowed_hosts

    def origin_ok(origin, environ=None) -> bool:
        return (urlsplit(origin).hostname or "") in allowed

    return origin_ok
