# Freshservice Managed MCP Server

A managed fork of [effytech/freshservice_mcp](https://github.com/effytech/freshservice_mcp) with controlled tool access via allowlists. Designed to run as a standalone Docker container on your network, accessible by any MCP-compatible client.

By default, only read/query tools are exposed. Write tools can be selectively enabled as needed.

## Quick Start (Docker)

> **The HTTP transport requires OAuth.** The server refuses to start without
> either a valid OAuth configuration or an explicit `MCP_ALLOW_INSECURE=true`
> opt-out. Exposing an MCP endpoint means exposing the Freshservice tenant to
> anyone who can reach the port — see [Security Model](#security-model).

### Pull and run from GitHub Container Registry

```bash
docker run -d \
  --name freshservice-mcp \
  -p 8080:8080 \
  -e FRESHSERVICE_APIKEY=your_api_key \
  -e FRESHSERVICE_DOMAIN=yourcompany.freshservice.com \
  -e MCP_OAUTH_ENABLED=true \
  -e MCP_OAUTH_ISSUER=https://auth.example.com \
  -e MCP_SERVER_URL=https://freshservice-mcp.example.com \
  -e MCP_READ_GROUPS=freshservice-readers \
  -e MCP_WRITE_GROUPS=freshservice-admins \
  ghcr.io/teejs/freshservice_mcp_managed:latest
```

The MCP endpoint is `${MCP_SERVER_URL}/mcp`. Health is `/healthz`, which stays
unauthenticated so container healthchecks keep working.

### Build from source

```bash
git clone https://github.com/TeeJS/freshservice_mcp_managed.git
cd freshservice_mcp_managed
docker build -t freshservice_mcp_managed .
docker run -d \
  --name freshservice-mcp \
  -p 8080:8080 \
  -e FRESHSERVICE_APIKEY=your_api_key \
  -e FRESHSERVICE_DOMAIN=yourcompany.freshservice.com \
  freshservice_mcp_managed
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FRESHSERVICE_APIKEY` | Yes | -- | Your Freshservice API key |
| `FRESHSERVICE_DOMAIN` | Yes | -- | Your Freshservice domain (e.g., `yourcompany.freshservice.com`) |
| `MCP_PORT` | No | `8080` | Port the MCP server listens on inside the container |
| `MCP_PATH` | No | `/mcp` | Path the MCP endpoint is served on |

#### Authentication

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCP_OAUTH_ENABLED` | Yes\* | `false` | Turns on the OAuth 2.1 resource server |
| `MCP_OAUTH_ISSUER` | If enabled | -- | Issuer URL, **byte-for-byte** as the provider reports it in its discovery document |
| `MCP_SERVER_URL` | If enabled | -- | Public base URL, no path. `MCP_SERVER_URL` + `MCP_PATH` is the `resource` value and must equal the connector URL exactly |
| `MCP_OAUTH_AUDIENCE` | Recommended | empty | The token's expected `aud`. **Empty skips audience validation** — any valid token the issuer minted for *another* client/resource is then accepted, so set this whenever the issuer serves more than this one resource. Enable **last**: an `aud` mismatch looks identical to every other 401 |
| `MCP_ALLOW_INSECURE` | No | `false` | Explicit opt-out that permits running with no authentication |

\* Either `MCP_OAUTH_ENABLED=true` or `MCP_ALLOW_INSECURE=true` must be set, or
the server exits at startup rather than silently coming up open.

#### Authorization

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCP_READ_GROUPS` | No | empty | Comma-separated groups granted the read tools |
| `MCP_WRITE_GROUPS` | No | empty | Comma-separated groups granted every registered tool |
| `MCP_OAUTH_GROUPS_CLAIM` | No | `groups` | Token claim holding group membership |

With **neither** list set there is no group policy and any authenticated caller
reaches every registered tool — the server logs a warning saying so at startup.
Once **either** is set, a valid token whose user matches no group gets `403`.

Values are whitespace-trimmed at load, because these get pasted by hand into
container UIs and an invisible tab on the issuer produces an unusable discovery
URL with an error pointing nowhere near the real mistake.

## Connecting MCP Clients

The server uses Streamable HTTP transport over HTTPS at `${MCP_SERVER_URL}/mcp`.

### claude.ai / Cowork (custom connector)

Add a **custom connector** pointing at `https://freshservice-mcp.example.com/mcp`.

If your identity provider does not advertise a `registration_endpoint`, Dynamic
Client Registration is unavailable and you must fill in the **Client ID and
Client Secret** fields. The claude.ai UI labels them optional; they are
mandatory in that case, and leaving them blank produces "Automatic client
registration isn't supported."

Register this callback URL on the OAuth client:

```
https://claude.ai/api/mcp/auth_callback
```

Never put a token in the connector URL. The MCP authorization spec prohibits
access tokens in the query string, and URLs leak through logs and history.

### Claude Code

```bash
claude mcp add freshservice --transport http https://freshservice-mcp.example.com/mcp
```

Claude Code uses a loopback redirect on an ephemeral port, so the OAuth client
must also accept `http://localhost/callback` and `http://127.0.0.1/callback`
with the port ignored.

### Verifying the deployment

Run this from **outside** your network. A reverse proxy with no matching host
returns `200` and a default landing page, so read the body, not just the status.

```bash
curl -s https://freshservice-mcp.example.com/healthz
```

Then confirm an anonymous caller is refused — this is the check that catches the
failure everything else misses:

```bash
curl -s -D - -o /dev/null -X POST https://freshservice-mcp.example.com/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

Expect `401` and **no** `mcp-session-id` header. A session id and a tool list
mean the server is open.

### NanoClaw/OpenClaw

Add to your NClaw MCP configuration:

```yaml
mcp_servers:
  freshservice:
    transport: streamable-http
    url: http://<your-host-ip>:8080/mcp
```

## How the Allowlist Works

This fork uses three sets in `server.py` to control which tools MCP clients can see:

- **`READONLY_TOOLS`** -- Read/query/filter/list tools. All enabled by default.
- **`ALLOWED_WRITE_TOOLS`** -- Write tools you've chosen to enable. Starts empty.
- **`DISABLED_WRITE_TOOLS`** -- Write tools that exist in the upstream code but are not exposed. This is your menu for future enablement.

Only tools in `READONLY_TOOLS` or `ALLOWED_WRITE_TOOLS` are registered with the MCP server. Everything else is invisible to AI clients.

### Enabling a Write Tool

1. Move the tool name from `DISABLED_WRITE_TOOLS` to `ALLOWED_WRITE_TOOLS` in `server.py`
2. Update your Freshservice API key's RBAC role to permit that action
3. Rebuild the Docker image and restart the container

## Security Model

Four layers, outermost first.

**1. Authentication (OAuth 2.1 resource server).** The `/mcp` endpoint requires a
valid JWT bearer token issued by your identity provider. An unauthenticated
request gets `401` with a `WWW-Authenticate: Bearer resource_metadata="..."`
header pointing at the discovery document, which is how an MCP client discovers
where to authenticate. `/healthz` and the two `.well-known` documents stay
outside the gate — gating discovery makes the flow undiscoverable and gating
health breaks the container healthcheck.

**2. Authorization (group-based tool gating).** A valid token proves who the
caller is, not what they may do. Group membership from the token maps onto a
read/write split enforced at **both** `tools/list` and `tools/call`. Filtering
the list alone is not a control, because a client can invoke a tool that was
never listed. A valid token with no mapped group gets `403`, not `401` —
re-authenticating would change nothing.

**3. The build-time allowlist.** Only tools in `READONLY_TOOLS` or
`ALLOWED_WRITE_TOOLS` are registered with the MCP server at all. This is an
allowlist rather than a denylist so that adding a tool and forgetting to
classify it fails closed.

**4. The Freshservice API key's RBAC role.** The last boundary: Freshservice
rejects any action the key lacks permission for, whatever the MCP server allows.
Create a custom role with only the permissions you need — see the
[Freshservice RBAC documentation](https://support.freshservice.com/en/support/solutions/articles/50000003741-agent-roles-in-freshservice).

### What the read tools actually expose

Worth being concrete, because "read-only" reads as harmless and is not. A caller
with read access can retrieve every ticket and full conversation thread (ticket
bodies routinely carry credentials and PII), the complete staff directory with
names, emails and phone numbers, and the full agent/requester group structure.
Treat read access as access to the ticket system, because that is what it is.

### Turning auth on breaks existing clients

The gate is on the process, not on one hostname. Every client still pointed at a
plain LAN URL starts getting `401`. That is correct behaviour and it still reads
as "the change broke it."

Claude Code is a native client using an RFC 8252 loopback redirect, so it
additionally needs `http://localhost/callback` and `http://127.0.0.1/callback`
registered on the OAuth client before it can authenticate at all.

## Available Tools (Read-Only)

### Tickets
| Tool | Description |
|------|-------------|
| `get_ticket_fields` | Get ticket form field definitions |
| `get_tickets` | List tickets with pagination |
| `filter_tickets` | Filter tickets by query |
| `get_ticket_by_id` | Get a single ticket by ID |

### Ticket Conversations
| Tool | Description |
|------|-------------|
| `list_all_ticket_conversation` | List conversations on a ticket |

### Ticket Tasks
| Tool | Description |
|------|-------------|
| `get_ticket_tasks` | List all tasks on a ticket (includes agent_id, group_id, status, due dates, stack rank) |
| `view_ticket_task` | Get a single ticket task by ID |

### Service Catalog
| Tool | Description |
|------|-------------|
| `list_service_items` | List service catalog items |
| `get_requested_items` | Get requested items on a ticket |

### Changes
| Tool | Description |
|------|-------------|
| `get_changes` | List changes with filtering |
| `filter_changes` | Filter changes by query |
| `get_change_by_id` | Get a single change by ID |
| `list_change_fields` | Get change form field definitions |

### Change Approvals
| Tool | Description |
|------|-------------|
| `list_change_approval_groups` | List approval groups on a change |
| `view_change_approval` | View a single approval |
| `list_change_approvals` | List all approvals on a change |

### Change Notes
| Tool | Description |
|------|-------------|
| `view_change_note` | View a single change note |
| `list_change_notes` | List notes on a change |

### Change Tasks
| Tool | Description |
|------|-------------|
| `view_change_task` | View a single change task |
| `get_change_tasks` | List tasks on a change |

### Change Time Entries
| Tool | Description |
|------|-------------|
| `view_change_time_entry` | View a single time entry |
| `list_change_time_entries` | List time entries on a change |

### Products
| Tool | Description |
|------|-------------|
| `get_all_products` | List products with pagination |
| `get_products_by_id` | Get a single product by ID |

### Requesters
| Tool | Description |
|------|-------------|
| `get_all_requesters` | List requesters with pagination |
| `get_requester_id` | Get a single requester by ID |
| `list_all_requester_fields` | Get requester field definitions |
| `filter_requesters` | Filter requesters by query |

### Agents
| Tool | Description |
|------|-------------|
| `get_agent` | Get a single agent by ID |
| `get_all_agents` | List agents with pagination |
| `get_agent_fields` | Get agent field definitions |
| `filter_agents` | Filter agents by query |

### Agent Groups
| Tool | Description |
|------|-------------|
| `get_all_agent_groups` | List all agent groups |
| `getAgentGroupById` | Get a single agent group by ID |

### Requester Groups
| Tool | Description |
|------|-------------|
| `get_all_requester_groups` | List requester groups |
| `get_requester_groups_by_id` | Get a single requester group by ID |
| `list_requester_group_members` | List members of a requester group |

### Canned Responses
| Tool | Description |
|------|-------------|
| `get_all_canned_response` | List canned responses |
| `get_canned_response` | Get a single canned response |
| `list_all_canned_response_folder` | List canned response folders |
| `list_canned_response_folder` | Get a single canned response folder |

### Solution Categories
| Tool | Description |
|------|-------------|
| `get_all_solution_category` | List solution categories |
| `get_solution_category` | Get a single solution category |

### Solution Folders
| Tool | Description |
|------|-------------|
| `get_list_of_solution_folder` | List folders in a category |
| `get_solution_folder` | Get a single solution folder |

### Solution Articles
| Tool | Description |
|------|-------------|
| `get_list_of_solution_article` | List articles in a folder |
| `get_solution_article` | Get a single solution article |

### Workspaces
| Tool | Description |
|------|-------------|
| `list_all_workspaces` | List all workspaces |
| `get_workspace` | Get a single workspace |

## Query Syntax for Filtering

When using `filter_tickets`, `filter_changes`, `get_changes`, or `filter_agents` with a `query` parameter, the query string must be wrapped in double quotes for the Freshservice API:

**Examples:**
- `"status:3"` -- Changes awaiting approval
- `"approval_status:1"` -- Approved changes
- `"approval_status:1 AND status:<6"` -- Approved changes that are not closed
- `"planned_start_date:>'2025-07-14'"` -- Changes starting after a specific date

## Example Operations

- "List all open tickets"
- "Get ticket #12345"
- "Filter tickets where status is pending"
- "Show all pending changes"
- "Get change tasks for change #5092"
- "List all agents in the support group"
- "Show solution articles in the FAQ folder"

## Upstream Tracking

This fork tracks [effytech/freshservice_mcp](https://github.com/effytech/freshservice_mcp). To pull upstream updates:

```bash
git fetch upstream
git merge upstream/main
```

New tools from upstream will not be exposed until they are added to the appropriate set in `server.py`. After merging, rebuild the Docker image.

## License

MIT License. See the LICENSE file for details.

## Credits

Based on [freshservice_mcp](https://github.com/effytech/freshservice_mcp) by [effy](https://effy.co.in/).
