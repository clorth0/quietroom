"""``quietroom-node --check-config``: is everything this config needs actually here?

Secrets live only in the environment (systemd ``EnvironmentFile``), so a config
can parse fine and still fail at 3 a.m. when the first alert needs the SMTP
password. This audit reports each env secret the config uses as required or
recommended and present or missing, never its value, and checks the fleet
mTLS material without side effects (no identity file is created).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quietroom.checks import Report, present
from quietroom.config import NodeConfig

SMTP_PASSWORD_ENV = "QUIETROOM_SMTP_PASSWORD"
WEBHOOK_SECRET_ENV = "QUIETROOM_WEBHOOK_SECRET"
CENTRAL_TOKEN_ENV = "QUIETROOM_CENTRAL_TOKEN"


def check_node_config(cfg: NodeConfig, env: Mapping[str, str]) -> dict[str, Any]:
    r = Report()
    if cfg.email is not None and cfg.email.username:
        r.secret(
            env, SMTP_PASSWORD_ENV, f"[alerts.email] SMTP login {cfg.email.username!r}",
            required=True,
            missing=f"[alerts.email] username {cfg.email.username!r} is set but "
            f"${SMTP_PASSWORD_ENV} is missing or blank: the node will refuse to start",
        )
    if cfg.webhook_url:
        r.secret(
            env, WEBHOOK_SECRET_ENV, "[alerts] webhook HMAC signature", required=False,
            missing=f"${WEBHOOK_SECRET_ENV} is not set: webhook alerts will be sent unsigned "
            "(the receiver cannot tell them from forgeries)",
        )
    fc = cfg.fleet
    if fc is not None and fc.auth == "legacy-token":
        if r.secret(
            env, CENTRAL_TOKEN_ENV, "[fleet] legacy shared bearer token", required=True,
            missing=f'[fleet] auth = "legacy-token" needs ${CENTRAL_TOKEN_ENV}',
        ):
            r.warnings.append(
                "fleet auth is the legacy shared bearer token (deprecated): any holder can "
                "impersonate any node; migrate to per-node mTLS"
            )
    elif fc is not None:
        if present(env, CENTRAL_TOKEN_ENV):
            r.warnings.append(
                f"${CENTRAL_TOKEN_ENV} is set but ignored under mTLS (it is never sent); "
                "remove it from the node's environment"
            )
        _check_mtls(cfg, r)
    return r.as_dict()


def _readable(path: str) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _check_mtls(cfg: NodeConfig, r: Report) -> None:
    from quietroom.fleet import cert_file_identity, client_tls_context
    from quietroom.update import check_secret_file

    fc = cfg.fleet
    assert fc is not None
    unreadable = [
        f"{k} {p}" for k, p in (
            ("cert_file", fc.cert_file), ("key_file", fc.key_file), ("ca_file", fc.ca_file)
        ) if not _readable(p)
    ]
    if unreadable:
        r.check("fleet mTLS material loads", False,
                f"[fleet] missing or unreadable: {', '.join(unreadable)}")
        return
    try:
        client_tls_context(fc.cert_file, fc.key_file, fc.ca_file)
    except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
        r.check("fleet mTLS material loads", False, f"[fleet] cannot load mTLS material: {e}")
        return
    r.check("fleet mTLS material loads", True)
    r.warnings += check_secret_file(fc.key_file)
    ident = Path(fc.identity_path)
    if not ident.exists():
        r.check(
            "client certificate UUID matches identity", False,
            f"[fleet] identity {ident} does not exist yet: run --print-identity (as the "
            "service user) and issue the client certificate for that UUID",
        )
        return
    try:
        node_uuid = str(json.loads(ident.read_text())["node_id"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        r.check("client certificate UUID matches identity", False, f"[fleet] {ident}: {e}")
        return
    try:
        cert_uuid = cert_file_identity(fc.cert_file)
    except NotImplementedError:
        r.warnings.append("cannot self-check the client certificate UUID on this Python")
        return
    if cert_uuid is None:
        problem = f"[fleet] {fc.cert_file} carries no node UUID (SAN urn:uuid:<uuid> or UUID CN)"
    elif cert_uuid != node_uuid:
        problem = (
            f"[fleet] client certificate is for node {cert_uuid} but this node is {node_uuid} "
            f"({ident})"
        )
    else:
        problem = ""
    r.check("client certificate UUID matches identity", not problem, problem)
