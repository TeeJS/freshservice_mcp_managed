# Deployment — freshservice-mcp behind Authelia

Naming follows the existing MCP clients already in the Authelia config.

| Thing | Value |
|---|---|
| Public URL / connector URL | `https://freshservice-mcp.example.com/mcp` |
| `resource` and client `audience` | the same string, **`/mcp` included** |
| Issuer | `https://auth.example.com` (bare origin, no trailing slash) |
| `client_id` | `freshservice-mcp` |
| `claims_policy` | `freshservice_mcp` |
| Write group | `freshservice-admins` |
| Read group | `freshservice-readers` |

### Where commands run

Every command below is marked. Getting this wrong is the easiest mistake here,
because the same directory has two different paths:

| | Path to Authelia's config |
|---|---|
| **On the Unraid host** (`root@host:~#`) | `/mnt/user/appdata/Authelia/` |
| **Inside the container** (`docker exec`) | `/config/` |

The container is named **`Authelia`** — capital A. `docker exec -it authelia`
fails with "No such container".

---

## 1. Generate the client secret — CONTAINER

```bash
docker exec -it Authelia authelia crypto hash generate pbkdf2 --variant sha512 --random --random.length 72 --random.charset rfc3986
```

This prints two values:

- **Random Password** — the plaintext secret. This goes into the claude.ai
  connector. Save it somewhere safe; it is not recoverable from the config.
- **Digest** (`$pbkdf2-sha512$...`) — the hash. This goes into
  `configuration.yml`.

**No signing key is needed.** Authelia signs all OIDC tokens with the provider
keypair already in `identity_providers.oidc.jwks`. That list is per-issuer, not
per-client — `key_id` is only a label. Do not generate a new keypair and do not
add a `jwks` entry for this client.

> If you ever do need to generate a keypair, `authelia crypto pair rsa generate
> --directory <dir>` **does not create the directory**. It fails with
> `open <dir>/private.pem: no such file or directory`. Create it first.

## 2. Back up the config — HOST

Matching the convention already used in that directory:

```bash
cp /mnt/user/appdata/Authelia/configuration.yml /mnt/user/appdata/Authelia/configuration.yml.bak-$(date +%Y%m%d-%H%M%S)
```

```bash
cp /mnt/user/appdata/Authelia/users_database.yml /mnt/user/appdata/Authelia/users_database.yml.bak-$(date +%Y%m%d-%H%M%S)
```

## 3. Edit `configuration.yml` — HOST

Full path: `/mnt/user/appdata/Authelia/configuration.yml`

Add a claims policy to the existing `claims_policies:` block, alongside the ones
already there:

```yaml
      freshservice_mcp:
        access_token:
          - 'groups'
```

Add a client to the existing `clients:` list:

```yaml
      - client_id: 'freshservice-mcp'
        client_name: 'Freshservice MCP'
        client_secret: '$pbkdf2-sha512$...'   # the Digest from step 1
        public: false
        authorization_policy: 'one_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        access_token_signed_response_alg: 'RS256'
        token_endpoint_auth_method: 'client_secret_post'
        claims_policy: 'freshservice_mcp'
        audience:
          - 'https://freshservice-mcp.example.com/mcp'
        redirect_uris:
          - 'https://claude.ai/api/mcp/auth_callback'
          - 'http://localhost/callback'
          - 'http://127.0.0.1/callback'
        scopes:
          - 'openid'
          - 'profile'
          - 'email'
          - 'address'
          - 'phone'
          - 'groups'
          - 'offline_access'
        grant_types:
          - 'authorization_code'
          - 'refresh_token'
        response_types:
          - 'code'
```

Why the non-obvious ones:

- `access_token_signed_response_alg: 'RS256'` — Authelia issues **opaque** access
  tokens by default, which cannot be validated statelessly at all.
- `claims_policy` — without it `groups` goes to `/userinfo` and never into the
  access token, so the server connects but shows only read tools.
- `token_endpoint_auth_method: 'client_secret_post'` — Claude uses POST body
  credentials. With `client_secret_basic` the login succeeds and the token
  exchange then fails with `invalid_client`.
- `audience` must whitelist the exact resource string, `/mcp` included, or
  Authelia refuses with `Requested audience ... has not been whitelisted`.
- The full seven-scope list — Claude requests every scope the AS advertises when
  not told otherwise, and Authelia rejects rather than ignores one the client is
  not permitted to request.
- The two loopback redirect URIs are only needed for Claude Code. Drop them if
  you will only ever connect from claude.ai.

## 4. Edit `users_database.yml` — HOST

Full path: `/mnt/user/appdata/Authelia/users_database.yml`

Add one line to your account's existing `groups:` list:

```yaml
      - 'freshservice-admins'
```

The result, with the surrounding structure for alignment. `users:` sits at
column 0, the username is indented 2, `groups:` is indented 4, and each group
item is indented 6:

```yaml
users:
  youruser:
    disabled: false
    displayname: '...'          # leave every existing line untouched
    password: '...'             # do not touch
    email: '...'
    groups:
      - 'existing-group-a'
      - 'existing-group-b'
      - 'freshservice-admins'   # <-- the only new line
```

Details that bite:

- **Six spaces of indentation**, matching the list items above. YAML is
  whitespace-sensitive and a misaligned item is the most common way this file
  breaks. Spaces only — a tab fails outright.
- **Match the surrounding quote style.**
- **Do not also add `freshservice-readers` to the same account.** Write access
  includes everything read access grants and the server checks the write group
  first, so listing both changes nothing. The read group matters only for a
  second account that should be limited to the read tools.
- **`MCP_READ_GROUPS` naming a group nobody is in is not an error.** It just
  means no one currently holds read-only access. The group does not need to
  exist anywhere for the server to start or work.

Authelia does **not** watch this file by default
(`authentication_backend.file.watch` is `false`), so the change has no effect
until the restart in step 5.

### Confirming the group reached the token

Do this **after** step 7, once the connector has been used at least once. Read
the MCP server's own authorization log — it records every decision, so no token
handling is needed:

```bash
docker logs freshservice-mcp 2>&1 | grep AUTHZ
```

Read the result off this table:

| Log line | Meaning | Fix |
|---|---|---|
| `AUTHZ_GRANTED ... permission=write groups=freshservice-admins` | Correct. Done. | — |
| `AUTHZ_GRANTED ... permission=read` | Account is in the read group, not the write group | Add `freshservice-admins` in step 4 |
| `AUTHZ_DENIED ... groups=<none in token>` | Groups never reached the token | `claims_policy` missing or wrong in `configuration.yml` (step 3) |
| `AUTHZ_DENIED ... groups=joplin-admins,...` | Token carries groups, but not a mapped one | Group missing from the account, or Authelia not restarted after step 4 |
| `OAUTH_TOKEN_REJECTED error=...` | Token failed validation | Usually an issuer mismatch — compare `MCP_OAUTH_ISSUER` byte-for-byte with the discovery document |
| nothing at all | The request never reached the server | Reverse proxy or DNS, not auth |

The two `AUTHZ_DENIED` variants are the pair that would otherwise be
indistinguishable — a missing claims policy on the provider versus a missing
group on the account. The `groups=` field separates them.

Do **not** try to decode the access token by hand. It is held by the connector,
and this server deliberately never logs bearer tokens, so there is nowhere to
obtain one from.

## 5. Validate, then restart — CONTAINER, then HOST

A bad config takes down SSO for everything behind Authelia, not just this
server. Validate before restarting, not after.

```bash
docker exec -it Authelia authelia validate-config --config /config/configuration.yml
```

Then, on the host:

```bash
docker restart Authelia
```

## 6. Run the container — HOST

```bash
docker run -d \
  --name freshservice-mcp \
  -p 8080:8080 \
  -e FRESHSERVICE_APIKEY=<key> \
  -e FRESHSERVICE_DOMAIN=yourcompany.freshservice.com \
  -e MCP_OAUTH_ENABLED=true \
  -e MCP_OAUTH_ISSUER=https://auth.example.com \
  -e MCP_SERVER_URL=https://freshservice-mcp.example.com \
  -e MCP_READ_GROUPS=freshservice-readers \
  -e MCP_WRITE_GROUPS=freshservice-admins \
  ghcr.io/teejs/freshservice_mcp_managed:latest
```

Leave `MCP_OAUTH_AUDIENCE` unset until the flow works end to end. Enabling it
early turns every failure into an identical, uninformative 401.

Point the reverse proxy for `freshservice-mcp.example.com` at this port, then
confirm the startup log says `MCP_AUTH_ENABLED` and not `MCP_AUTH_DISABLED`:

```bash
docker logs freshservice-mcp | head -20
```

## 7. claude.ai connector

Add a **custom connector** at `https://freshservice-mcp.example.com/mcp`,
filling in Client ID `freshservice-mcp` and the plaintext secret from step 1.

If your Authelia advertises no `registration_endpoint`, Dynamic Client
Registration is unavailable and those fields — labelled optional in the UI — are
mandatory. Leaving them blank produces "Automatic client registration isn't
supported."

## 8. Verify from outside the network

Testing from the LAN proves nothing about what the internet can reach.

```bash
curl -s https://freshservice-mcp.example.com/healthz
```

Expect the app's own JSON, not HTML. HTML means the hostname is not routed to
the container, regardless of the status code.

```bash
curl -s -D - -o /dev/null -X POST https://freshservice-mcp.example.com/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

Expect `401` and **no** `mcp-session-id` header.

If the connector fails, the client-side message is always the same opaque
"Authorization with the MCP server failed" and never names the cause. Authelia's
log does — read it first:

```bash
docker logs Authelia --tail 50
```

To check the client credentials without an interactive login, send a
deliberately invalid authorization code:

```bash
curl -s -d "client_id=freshservice-mcp&client_secret=<plaintext>&grant_type=authorization_code&code=bad&redirect_uri=https://claude.ai/api/mcp/auth_callback" https://auth.example.com/api/oidc/token
```

`invalid_grant` means client authentication succeeded and only the fake code was
rejected — the secret and auth method are right. `invalid_client` means the
secret or `token_endpoint_auth_method` is wrong.

## Done when

1. Unauthenticated `initialize` returns 401 with no session id.
2. `/healthz` and both `.well-known` paths answer unauthenticated.
3. An authorized caller completes one real tool call against Freshservice.
4. A read-scoped caller sees only read tools and is refused if it calls a write
   tool by name.
5. A valid token with no mapped group gets 403.
