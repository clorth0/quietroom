"""The one token-acceptance policy for the web UI.

Both surfaces check the same configured token (``QUIETROOM_TOKEN``, owned
by :mod:`quietroom.web.bind`); they differ only in where a client may
present it, which is a call-site fact, not a second policy:

* HTTP requests present the query token, else the ``quietroom_token``
  cookie. A present query token shadows the cookie.
* A Socket.IO connect may additionally present ``auth={"token": ...}``
  (for non-browser clients); a wrong auth token falls through to the
  same query/cookie presentation. The browser client presents no auth
  and relies on the query-then-cookie flow.

No configured token means no check (loopback default; a remote bind
already required a token before this module is consulted).
``X-Forwarded-For`` is never consulted: acceptance is token-only.
"""
from __future__ import annotations

import hmac

TOKEN_COOKIE = "quietroom_token"


def token_matches(given: object, token: str) -> bool:
    """The single constant-time comparison every surface funnels into."""
    return hmac.compare_digest(str(given).encode(), token.encode())


def token_accepted(
    token: str | None,
    *,
    auth_token: object = None,
    query_token: str | None = None,
    cookie_token: str | None = None,
) -> bool:
    """Accept exactly what the two pre-unification checks accepted.

    A truthy ``auth_token`` is tried first and, if wrong, does not block
    the fallback. The fallback is the query token if present, else the
    cookie token, else empty (which never matches a configured token).
    """
    if token is None:
        return True
    if auth_token and token_matches(auth_token, token):
        return True
    presented = query_token or cookie_token or ""
    return token_matches(presented, token)


def request_token_accepted(token: str | None, *, auth_token: object = None) -> bool:
    """``token_accepted`` with the presentation read from the Flask request."""
    from flask import request

    return token_accepted(
        token,
        auth_token=auth_token,
        query_token=request.args.get("token"),
        cookie_token=request.cookies.get(TOKEN_COOKIE),
    )
