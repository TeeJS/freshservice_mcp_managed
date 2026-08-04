# Deployment — freshservice-mcp behind Authelia

Concrete values for this environment. Naming follows the existing `service-a-mcp`
and `service-b-mcp` clients.

| Thing | Value |
|---|---|
| Public URL / connector URL | `https://freshservice-mcp.example.com/mcp` |
| `resource` and client `audience` | the same string, **`/mcp` included** |
| Issuer | `https://auth.example.com` (bare origin, no trailing slash) |
| `client_id` | `freshservice-mcp` |
| `claims_policy` | `freshservice_mcp` |
| `jwks` `key_id` | `freshservice-mcp` |
| Write group | `freshservice-admins` |
| Read group | `freshservice-readers` |

---

## 1. Generate the key and secret — inside the Authelia container

These must be generated on the Unraid box. **Do not paste the private key or the
plaintext secret back into a chat session** — a private key that signs access
tokens lets anyone mint tokens your resource servers will accept.

```bash
docker exec -it authelia authelia crypto pair rsa generate --bits 4096 --directory /config/oidc/freshservice-mcp
```

```bash
docker exec -it authelia authelia crypto hash generate pbkdf2 --variant sha512 --random --random.length 72 --random.charset rfc3986
```

The second command prints a **Random Password** (the plaintext secret — this is
what goes into the claude.ai connector) and a **Digest** (the `$pbkdf2-sha512$...`
hash — this is what goes into `configuration.yml`). Write the plaintext to a
`chmod 600` file rather than leaving it in scrollback.

## 2. Back up the config first

Matching the convention already in use in that directory:

```bash
cp /config/configuration.yml /config/configuration.yml.bak-$(date +%Y%m%d-%H%M%S)
```

## 3. `configuration.yml`

Add a claims policy under the existing `claims_policies:` block, alongside
`service_a_mcp` and `service_b_mcp`:

```yaml
      freshservice_mcp:
        access_token:
          - 'groups'
```

Add the signing key to the existing `jwks:` list:

```yaml
      - key_id: 'freshservice-mcp'
        algorithm: 'RS256'
        use: 'sig'
        key: |
          -----BEGIN PRIVATE KEY-----
          <contents of /config/oidc/freshservice-mcp/private.pem>
          -----END PRIVATE KEY-----
```

Add the client to the existing `clients:` list:

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

Why each of the non-obvious ones:

- `access_token_signed_response_alg: 'RS256'` — Authelia issues **opaque** access
  tokens by default, which cannot be validated statelessly at all.
- `claims_policy` — without it `groups` goes to `/userinfo` and not into the
  access token, so the server connects but shows only read tools.
- `token_endpoint_auth_method: 'client_secret_post'` — Claude uses POST body
  credentials. With `client_secret_basic` the login succeeds and the token
  exchange then fails with `invalid_client`.
- `audience` must whitelist the exact resource string, `/mcp` included, or
  Authelia refuses with `Requested audience ... has not been whitelisted`.
- The full seven-scope list — Claude requests every scope the AS advertises when
  not told otherwise, and Authelia rejects rather than ignores one the client may
  not request.
- The two loopback redirect URIs are only needed for Claude Code. Drop them if
  you will only ever connect from claude.ai.

## 4. `users_database.yml`

Add both groups to `youruser`. Without the group the server connects and shows
nothing, which reads as a bug:

```yaml
    groups:
      - 'service-a-admins'
      - 'service-b-admins'
      - 'freshservice-admins'
```

Authelia does **not** watch this file by default (`authentication_backend.file.watch`
is `false`), so a group added without a restart has no effect.

## 5. Validate before restarting

A bad config takes down SSO for everything behind Authelia, not just this server.

```bash
docker exec -it authelia authelia validate-config --config /config/configuration.yml
```

Then restart Authelia.

## 6. Run the container

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

Point the reverse proxy for `freshservice-mcp.example.com` at this port.
Confirm the startup log says `MCP_AUTH_ENABLED` and not `MCP_AUTH_DISABLED`.

## 7. claude.ai connector

Add a **custom connector** at `https://freshservice-mcp.example.com/mcp`,
filling in Client ID `freshservice-mcp` and the plaintext secret from step 1.

Your Authelia has **no `registration_endpoint`**, so Dynamic Client Registration
is unavailable and those fields — labelled optional in the UI — are mandatory.
Leaving them blank produces "Automatic client registration isn't supported."

## 8. Verify, from outside the network

```bash
curl -s https://freshservice-mcp.example.com/healthz
```

Expect the app's own JSON, not HTML. HTML means the hostname is not routed to
the container regardless of the status code.

```bash
curl -s -D - -o /dev/null -X POST https://freshservice-mcp.example.com/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

Expect `401` and **no** `mcp-session-id` header.

If the connector fails, the client-side message is always the same opaque
"Authorization with the MCP server failed" and never names the cause. Authelia's
log does. Read it first.

To check the client credentials without an interactive login, send a deliberately
invalid code:

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
