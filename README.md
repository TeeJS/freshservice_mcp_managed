# Freshservice Managed MCP Server

A managed fork of [effytech/freshservice_mcp](https://github.com/effytech/freshservice_mcp) with controlled tool access via allowlists. Designed to run as a standalone Docker container on your network, accessible by any MCP-compatible client.

By default, only read/query tools are exposed. Write tools can be selectively enabled as needed.

## Quick Start (Docker)

### Pull and run from GitHub Container Registry

```bash
docker run -d \
  --name freshservice-mcp \
  -p 8080:8080 \
  -e FRESHSERVICE_APIKEY=your_api_key \
  -e FRESHSERVICE_DOMAIN=yourcompany.freshservice.com \
  ghcr.io/teejs/freshservice_mcp_managed:latest
```

The MCP server will be available at `http://<your-host-ip>:8080/mcp`.

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

## Connecting MCP Clients

The server uses Streamable HTTP transport. Clients connect to `http://<host-ip>:<port>/mcp`.

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
"mcpServers": {
  "freshservice": {
    "type": "streamable-http",
    "url": "http://<your-host-ip>:8080/mcp"
  }
}
```

### Claude Code

```bash
claude mcp add freshservice --transport streamable-http http://<your-host-ip>:8080/mcp
```

### NanoClaw

Add to your NanoClaw MCP configuration:

```yaml
mcp_servers:
  freshservice:
    transport: streamable-http
    url: http://<your-host-ip>:8080/mcp
```

### UNRAID Setup

1. In the UNRAID Docker UI, add a new container
2. Set the repository to `ghcr.io/teejs/freshservice_mcp_managed:latest`
3. Add the environment variables `FRESHSERVICE_APIKEY` and `FRESHSERVICE_DOMAIN`
4. Map container port `8080` to your desired host port
5. Start the container

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

The Freshservice API key's RBAC role is the primary security boundary. Even if a tool is exposed via the MCP, Freshservice will reject any action the API key doesn't have permission for. The allowlist is defense-in-depth -- it prevents the AI from even attempting operations you haven't approved.

**Recommended**: Create a custom Freshservice role with only the permissions you need. See [Freshservice RBAC documentation](https://support.freshservice.com/en/support/solutions/articles/50000003741-agent-roles-in-freshservice).

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
