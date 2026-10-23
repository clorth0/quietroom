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

## Not covered yet

- No CRL/OCSP. Revoke by issuing short-lived certificates and rotating the node CA. A deny-list of UUIDs at central is a small follow-up.
- `quietroom-central` serves through Werkzeug's threaded server, because that is where the verified peer certificate is available. It is fine for a small fleet. A terminating reverse proxy would need a trusted identity-header design, which is not implemented on purpose.
