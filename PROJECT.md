# PROJECT CHARTER — Securing freshservice_mcp_managed

**Status:** DRAFT — awaiting sign-off
**Created:** 2026-08-03

---

## Phase 0 findings (established, no changes made)

### What the server is today

| Fact | Value |
|---|---|
| Transport | streamable-HTTP, `mcp.run(transport='streamable-http')` |
| Bind address | `0.0.0.0:8080`, path `/mcp` ([server.py:24](src/freshservice_mcp/server.py:24)) |
| Authentication | **None.** No token, no OAuth, no middleware hook. |
| Transport security | Plain HTTP — README documents `http://<host-ip>:8080/mcp` |
| Registered tools | 22 read tools |
| Registered write tools | **0** — `ALLOWED_WRITE_TOOLS = set()` ([server.py:251](src/freshservice_mcp/server.py:251)) |
| Write functions in code | 39, all skipped by `allowed_tool()` before reaching `mcp.tool()` |
| Credential held | `FRESHSERVICE_APIKEY` (HTTP Basic) for a live corporate tenant |
| Deployment | Unraid host, image from `ghcr.io/teejs/freshservice_mcp_managed` |

### Blast radius for anyone who can reach port 8080

Anonymous, unauthenticated, scoped only by the API key's Freshservice RBAC role:

- **All tickets and full conversation threads** — `get_tickets`, `filter_tickets`,
  `get_ticket_by_id`, `list_all_ticket_conversation`. Ticket bodies routinely carry
  credentials, PII, and vendor detail.
- **Complete staff directory** — `get_all_requesters`, `filter_requesters`,
  `get_all_agents`, `filter_agents`: names, emails, phone numbers, job titles.
- **Org structure** — agent groups, requester groups and their members, workspaces.

This is a **disclosure** risk, not an integrity risk. No registered tool can create,
modify, or delete anything.

### Two things that are NOT wrong (checked, so they can stop being worried about)

- **No path-taking tool.** No `open()`, no import-from-file, no attachment tool.
  The arbitrary-file-read → `/proc/self/environ` → service-credential-disclosure
  vector does not exist here. Every tool is a fixed `httpx` call to the Freshservice API.
- **The API key is not in git.** It appears in `.claude/settings.local.json`, which is
  covered by a global gitignore and absent from all history. Local hygiene issue only,
  not a published leak.

### Latent break that intersects this work

`pyproject.toml` declares `mcp[cli]>=1.3.0` with **no upper bound**. `uv.lock` pins
1.6.0, but the Dockerfile runs `pip install .` and never uses the lockfile — so every
image build re-resolves from PyPI. **`mcp` 2.0.0 is the current PyPI release and it
removed `mcp.server.fastmcp`.** The next CI build therefore produces an image that
dies at import, with or without this security work. Since securing the server requires
a rebuild, the pin must land in the same change or the fix will look like it caused
the outage.

Same class of issue: `from dotenv import load_dotenv` ([server.py:14](src/freshservice_mcp/server.py:14))
with `python-dotenv` undeclared — it currently arrives only as a transitive dep of `mcp[cli]`.

### Deployment facts (answered 2026-08-03, now recorded in the skill)

- **Public, behind a reverse proxy** on `*.example.com`. Has been for years.
- **claude.ai / Cowork must reach it** → OAuth is mandatory, not optional.
- **No panic mitigation.** Nothing here is newly exposed; build the real thing properly
  rather than flipping a read-only switch as a stand-in.
- **IdP: Authelia**, issuer `https://auth.example.com` (bare origin, no trailing
  slash — read from the live discovery document, not assumed).
  Config at `<your Authelia config directory>`.
- **No `registration_endpoint`** → DCR unavailable. This must be a **custom connector
  with a manually-entered Client ID and Secret**; the claude.ai fields labelled
  *optional* are mandatory here.
- **Public URL for this server:** `https://freshservice-mcp.example.com/mcp`
  (following the existing `service-a-mcp` / `service-b-mcp` pattern). This exact string,
  `/mcp` included, is the `resource` and the client `audience`.

---

## The five charter questions

### 1. What is the one thing this must do?

Make it so that reaching the MCP endpoint is not the same as reading the company's
Freshservice tenant. A caller must prove who they are before any tool is listed or
called, and what they may do must follow from their identity — not from having found
the port.

### 2. What would be wrong if we shipped "working" software without it?

A server that answers `initialize` and returns a tool list to an anonymous caller.
Green healthcheck, running container, working Claude connector — and still an open
read API for the ticket system. **Verification is by attacking it from outside the
network, not by a 200 from a health endpoint.**

### 3. What is explicitly off-limits as a workaround?

- **Obscurity as a control.** "The hostname is unguessable" is not authentication.
- **A token in the connector URL / query string.** Prohibited by the MCP auth spec,
  and URLs leak through logs, proxies, and history.
- **Filtering `tools/list` only.** A client can call a tool that was never listed.
  Gating must be enforced at `tools/call` as well.
- **A denylist of write tools.** The split must be an allowlist of read tools, so it
  fails closed when a tool is added and nobody classifies it.
- **Relying on `ALLOWED_WRITE_TOOLS` being empty as the security boundary.** It is a
  build-time convenience, not an access control; the day one write tool is enabled it
  is enabled for every anonymous caller.
- **Logging the request object.** MCP request structs embed the full header map — a
  `log(request)` writes replayable bearer tokens to disk on every call once auth is on.

### 4. Deployment target and backup location

- **Target:** Unraid host, via `ghcr.io/teejs/freshservice_mcp_managed:latest`,
  rebuilt by the existing GitHub Actions workflow on push to `main`.
- **Backup — source:** the git repo covers it. Working on a branch, not `main`.
- **Backup — identity-provider config:** lives outside this repo. Default assumption:
  timestamped copy alongside the original on the host before edit, and the config
  validated before any restart — a bad IdP config takes down SSO for everything
  behind it, not just this server. **Confirm the IdP config path at sign-off.**

### 5. How will we verify it is done?

Not done until all five pass, run **from outside the network**:

1. Unauthenticated `initialize` against `/mcp` returns **401**, no session id, and a
   `WWW-Authenticate: Bearer resource_metadata="…", scope="…"` header.
2. `/healthz` and both `.well-known` paths still answer **unauthenticated**, and
   `authorization_servers[0]` is byte-clean (no pasted whitespace).
3. An authorized caller completes **one real tool call** end to end against the live
   Freshservice tenant.
4. A read-scoped caller sees only read tools **and** is refused with an error when it
   calls a write tool by name anyway.
5. A valid token whose user is in no mapped group gets **403**, not 401.

---

## Proposed plan

| # | Step | Needs from user |
|---|---|---|
| 1 | Pin `mcp[cli]>=1.28.1,<2`, declare `python-dotenv`, make the Dockerfile install the lockfile | none |
| 2 | Build the ASGI app instead of `mcp.run()`; add `/healthz` + `.well-known` routes outside the gate | none |
| 3 | OAuth resource server: JWT validation, 401 + `resource_metadata`, mirrored discovery, config trimming, redacted logging, startup posture log | none |
| 4 | Tool gating: read allowlist enforced at both `tools/list` and `tools/call`; 403 for authz failure; policy narrows only | none |
| 5 | `MCP_OAUTH_ENABLED=false` by default — one image, both postures; LAN deployments unchanged until switched on | none |
| 6 | Identity-provider client config, written out ready to apply | IdP type + admin access |
| 7 | Verify by attacking from outside | public hostname |

Steps 1–5 are code in this repo and need no input. Steps 6–7 need the deployment facts.

## Decisions taken by default (change any in one line)

- **Groups:** `freshservice-admins` → write, `freshservice-readers` → read — matching the
  existing `service-a-admins` / `service-b-admins` convention (bare service name, no `-mcp-`).
  Read via `MCP_READ_GROUPS` / `MCP_WRITE_GROUPS` env vars; unset means no group policy.
- **`client_id`:** `freshservice-mcp`; **`claims_policy`:** `freshservice_mcp`
- **Audience validation:** off initially (`MCP_OAUTH_AUDIENCE` empty), enabled last —
  an `aud` mismatch is indistinguishable from every other 401.
- **Auth default:** off, so existing LAN clients keep working until the switch is thrown.

## Known consequence to state plainly

Turning auth on gates the **process**, not one hostname. Every existing client — the
Claude Desktop entry, the Claude Code registration, NanoClaw — starts getting 401 at
its LAN URL. That is correct behaviour and it still reads as "the change broke it."
Claude Code additionally needs `http://localhost/callback` and `http://127.0.0.1/callback`
registered on the IdP client before it can authenticate at all.
