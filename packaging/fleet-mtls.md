# Fleet mTLS: per-node identity

Each node authenticates to central with **its own client certificate**. The
certificate's identity is the node's UUID, and central binds every heartbeat or
event to it. This replaces the shared `QUIETROOM_CENTRAL_TOKEN`: anyone holding
that token could post as any `node_id`, and revoking it meant re-keying the
whole fleet.

| Side | Presents | Trusts (pinned, no system roots) |
|---|---|---|
| node (`[fleet]`) | `cert_file`/`key_file`: node cert, SAN `urn:uuid:<node uuid>` | `ca_file`: the CA that issued central's server cert |
| central (`quietroom-central`) | `--cert`/`--key`: server cert for its hostname | `--client-ca`: the CA that issues node certs |

Identity rules (central, `quietroom.fleet.identity_from_peer_cert`):
- The UUID comes from a SAN URI `urn:uuid:<uuid>`, or from a CN that is a UUID.
- A cert with several distinct UUIDs, or a UUID CN that contradicts its SAN, is refused.
- Every write must carry `node_uuid` equal to the certificate's UUID (FleetClient adds it). Any other value gets 403.
- The stored record is stamped with the certificate's UUID and `auth: "mtls"`. A `node_uuid`/`auth` field in an event body is overwritten.
- `GET /api/v1/*` is for operators and needs `QUIETROOM_CENTRAL_READ_TOKEN`. A node certificate cannot read the fleet.
- `quietroom-central` makes client certificates mandatory at the TLS layer unless certificate-less access is configured (`QUIETROOM_CENTRAL_READ_TOKEN` for operator reads, or `--allow-legacy-token`). In that case it asks for a certificate but does not require one: a certificate that is presented must still chain to `--client-ca`, and every certificate-less write without the legacy token gets 403 from the app.

## Provisioning (OpenSSL 3)

Keep the node CA key offline. The commands below are what the PR's run proof executes.

```sh
# once: node-issuing CA (central's server cert may come from a separate CA)
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -subj "/CN=quietroom node CA" -keyout node-ca.key -out node-ca.crt \
  -addext basicConstraints=critical,CA:TRUE,pathlen:0 -addext keyUsage=critical,keyCertSign,cRLSign

# on the node: get (or create) its UUID
UUID=$(sudo -u quietroom quietroom-node --config /etc/quietroom/config.toml --print-identity)

# on the node: key + CSR (the key never leaves the node)
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -subj "/CN=$UUID" -keyout node.key -out node.csr
chmod 600 node.key

# at the CA: sign it, binding the UUID in the SAN, client-auth only, short-lived
openssl x509 -req -in node.csr -CA node-ca.crt -CAkey node-ca.key -days 90 -out node.crt \
  -extfile <(printf 'basicConstraints=critical,CA:FALSE\nextendedKeyUsage=clientAuth\nsubjectAltName=URI:urn:uuid:%s\n' "$UUID")
```

Install it as `/etc/quietroom/fleet/node.{crt,key}` (key `0600`, owned by
`quietroom`) along with `central-ca.crt`. `quietroom-node` refuses to start
if the certificate's UUID is not this node's UUID, and warns (in
`health.fleet_warnings`) if the key is group- or world-readable.

## Migration from the shared token

`[fleet] auth` defaults to `"mtls"`. An existing config that has only
`base_url` now **fails to load**, and the error says how to proceed. To
migrate without downtime:

1. Start central with `--allow-legacy-token` and `QUIETROOM_CENTRAL_TOKEN`
   set. It accepts certificate-less nodes bearing the token, records them as
   `auth: "legacy-token"` and answers with `X-Quietroom-Auth: legacy-token`.
   mTLS nodes work on the same listener.
2. On nodes not yet migrated, set `auth = "legacy-token"` explicitly. They
   report `health.fleet_auth == "legacy-token"` with a deprecation warning.
3. Issue certificates node by node and switch each one to mTLS config.
4. When `GET /api/v1/nodes` shows no `legacy-token` nodes, restart central
   without `--allow-legacy-token` (client certificates then become mandatory
   at the TLS layer) and destroy the shared token.

## Revoking a node (deny list)

Start central with `--deny-list /etc/quietroom/central/deny.txt`. The file holds one
node UUID per line, and `#` starts a comment:

```text
# 2026-10-22 floor3-east: chassis stolen, key presumed compromised
6f1c2a52-6a0e-4c35-9a53-0b8a3a6f5e01
```

- **No restart needed.** Central `stat`s the file on every write and re-reads it
  when inode, size, mtime or ctime change. The next request from a listed UUID gets
  `403 node certificate revoked`, and its heartbeats and events are not stored.
  Central logs the first refusal for each UUID to stderr.
- **Check it before relying on it.** `quietroom-central ... --deny-list FILE
  --check-config` fails (exit 1) if the file is missing, a directory, not UTF-8,
  unreadable, or holds a line that is not a UUID (it names the line). On success it
  reports how many UUIDs are revoked.
- **Edit atomically.** Write a temporary file in the same directory, then rename it
  over the list, so central never reads a half-written file:
  `cp deny.txt deny.txt.new && echo "$UUID" >> deny.txt.new && mv deny.txt.new deny.txt`.
- **It fails closed.** A missing or malformed file stops central at startup. If the
  file later disappears or stops parsing, central answers **all** writes with 503.
  This includes legacy-token writes. `/healthz` reports `"ok": false` and the error
  until the file is fixed. Nodes keep their data in their outboxes meanwhile, and
  operator reads keep working. Central doesn't fall back to the previous list,
  because then a revocation that was being added could silently fail to apply. An
  empty file means "nobody is revoked".
- `GET /api/v1/nodes` marks each node `"revoked": true|false`, and
  `/healthz.deny_list` shows `{path, entries, error}`.
- **The TLS handshake still succeeds.** A revoked node's certificate still chains
  to `--client-ca`. Refusal happens per request in the app, so a revoked node can
  still open connections. Its writes are refused, and it can never read the fleet:
  node certificates never could.
- **The deny list can't revoke the legacy shared token.** That token carries no
  node identity, so the only way to revoke it is to rotate it, which affects every
  legacy node.
- **After a key compromise,** don't just re-issue a certificate for the same UUID:
  the denied UUID stays denied. Give the replacement node a new identity (delete
  `identity.json`, run `--print-identity`), issue a certificate for the new UUID,
  and leave the old UUID on the list. Short certificate lifetimes (e.g. 90 days)
  remain the backstop.

## Certificate expiry

- **Node.** `[fleet] cert_warn_days` (default 30; 0 disables the warning).
  `quietroom-node` reads its certificate's `notAfter` at startup and reports
  `health.fleet_cert = {not_after, expires_in_s, expiring, expired}`. The heartbeat
  carries the same object. Inside the window, `health.fleet_warnings` gains
  `client certificate expires in N days (...)`. The node re-evaluates this on every
  health call, so a long-running daemon starts warning without a restart. It also
  prints the warning to stderr at startup.
- **Central.** `--cert-warn-days` (default 30; a whole number of days from 1 to 365,
  checked by `--check-config` and at startup, where a bad value exits 2. Unlike
  the node setting, 0 is not accepted). Central records `notAfter` from the
  **verified TLS client certificate** on each heartbeat, never from the body.
  `GET /api/v1/nodes` gives each node
  `cert: {not_after, expires_in_s, expiring, expired}`, which is `null` for
  legacy-token nodes. `/healthz.certs_expiring` counts nodes inside the window.
  Central logs one stderr warning per node.
- **Renewal.** Issue a new certificate for the **same** UUID with the CSR steps
  above, install it, and restart `quietroom-node`.

## Reverse proxies

**Supported: TCP (L4) passthrough.** The proxy forwards the raw TLS stream and
central still terminates TLS and verifies the client certificate itself. Nothing
about identity changes. HAProxy:

```haproxy
frontend quietroom_fleet
    mode tcp
    bind :8443
    default_backend quietroom_central
backend quietroom_central
    mode tcp
    server central 127.0.0.1:9443
```

The nginx equivalent is a `stream { server { listen 8443; proxy_pass 127.0.0.1:9443; } }`
block. To share port 443 with other sites, route by SNI with `ssl_preread on`. Central's
server certificate must name the hostname nodes connect to. Central sees the
proxy's address as the client IP, which doesn't matter for identity: identity is
never derived from the IP.

**Declined: TLS-terminating proxies that pass the client certificate in a header**
(e.g. nginx `$ssl_client_escaped_cert`, or `X-SSL-Client-Cert` / `X-Forwarded-Client-Cert`).
Central takes identity only from the TLS socket. `PEER_CERT_ENVIRON` can't be set from
HTTP, and `test_http_header_cannot_inject_a_peer_cert` checks that. Honouring a
forwarded-certificate header would make node identity depend on two things:

- every request reaching central through a proxy that strips and replaces the header;
- nothing else being able to reach central's port.

If either assumption fails (a second listener, a direct port, a proxy route that
forgets to clear the header), anyone could claim any node UUID. Doing it safely needs
a second authenticated hop, i.e. proxy-to-central mTLS plus an allow-list of proxy
identities. That is more trust surface than the feature is worth for a small fleet,
so it isn't implemented.

## Legacy token: deprecation and removal plan

The shared `QUIETROOM_CENTRAL_TOKEN` (`[fleet] auth = "legacy-token"` on nodes,
`--allow-legacy-token` on central) has been deprecated since #52, 2026-10-22. It
still works, unchanged, for migration. **Removal is planned for quietroom 0.5.0,
not before 2027-04-01.** Until then:

- central prints a DEPRECATED warning with this plan at startup when
  `--allow-legacy-token` is set;
- legacy-token responses carry `X-Quietroom-Auth: legacy-token` and
  `Deprecation: @1792627200` (RFC 9745). They also carry
  `Sunset: Thu, 01 Apr 2027 00:00:00 GMT` (RFC 8594) and a `Link` to this section
  with `rel="deprecation"`;
- `/healthz.legacy_token_writes` counts accepted legacy writes since start. When it
  stays at 0 and `GET /api/v1/nodes` shows no `auth: "legacy-token"` nodes, follow
  step 4 of the migration;
- legacy-mode nodes print the warning with the plan to stderr at startup, and keep
  it in `health.fleet_warnings`.

## Not covered yet

- No CRL/OCSP. Revocation is the deny list above plus short-lived certificates.
- `quietroom-central` serves through Werkzeug's threaded server, because that is where the verified peer certificate is available. It is fine for a small fleet. Use L4 passthrough (above) if it must sit behind a proxy.
- A revoked node keeps retrying, one attempt per tick. Its fleet outbox (`<store>.fleet-outbox`) is bounded (`[fleet] outbox_max_rows`, `outbox_max_age_s`; see packaging/hardening.md), so its data no longer piles up without limit, but the oldest data is parked and eventually dropped until the node is decommissioned or re-issued a certificate.
