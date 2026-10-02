# PROJECT CHARTER — Per-user Freshservice keys in Open WebUI

**Status:** REVISION 3 — APPROVED 2026-10-01. Steps 1–7 done; Rosie runs `sha-822430f` (fix: dropped an unverified key-format check that refused a real key). Verification (2026-10-02): **1 ✅** your key → ticket 47071's 21 tasks; **4 ✅** task 5589 Completed→Open→Completed, both credited to T.J. Schmitz in Freshservice Activity; **5 ✅** 0 auth headers/tokens/keys in logs, `KEY_LOOKUP result=found`; **2 ✅** an Open WebUI account with no saved key gets the "add your key" message (Controls > Valves > Tools > Freshservice Key). Open: 3 (second person's key), 6 (Unraid on new `latest`).
**Progress:** per-user mode now serves MCP and reads each user's key from Open WebUI. `tests/test-per-user-mode.py`: 23/23 pass, using the real MCP protocol against fake Open WebUI and Freshservice. That includes the Unraid checks (shared mode: same 22 tools, still uses `FRESHSERVICE_APIKEY`). The Freshservice Key tool is in `owui/freshservice_key.py`.
**Created:** 2026-10-01
**Revision 3 (2026-10-01):** replaces the browser-based design. The server now runs exactly like the other MCPs on Rosie (Docker network, no ports, added in Open WebUI admin). Each user's key comes from a private per-user box in Open WebUI.

---

## Terms used in this charter

| Term | Plain meaning |
|---|---|
| **MCP** | The connection type your QlikSense entry uses in Open WebUI. Our Freshservice server will be added the same way. |
| **Docker network** | The private network Rosie's containers share (`infra-net`). Containers on it reach each other by name, e.g. `open-webui`, `freshservice-tools`, with no port opened on Rosie. Open WebUI and nginx-proxy are already on it. |
| **Auth: Session** | A choice in the **Auth** dropdown when you add a tool server in Open WebUI. It makes Open WebUI send the logged-in user's own **login token** with every call. |
| **Login token** | What Open WebUI gives your browser when you log in. It proves who you are. Our server only uses it to ask Open WebUI for that user's key. |
| **Valves** | Open WebUI's name for a tool's settings. **User valves** are settings each user fills in for themselves, privately. |
| **Controls** | The panel that opens from the icon at the top right of a chat window. **Valves** is a section inside it. |
| **Image tag** | The version label of the container image. Our branch build is `sha-…`; Unraid uses `latest`. |
| **`OWUI_URL`** | A setting telling our server where Open WebUI is on the Docker network: `http://open-webui:8080`. |

---

## Every change to the work environment

This is the complete list. Nothing else changes.

| # | Where | Change | Who |
|---|---|---|---|
| 1 | Rosie, Dockge stack `freshservice-tools` (already exists) | New image tag, plus one setting: `OWUI_URL=http://open-webui:8080`. **No ports.** | You |
| 2 | Open WebUI → Admin Settings → Integrations → External Tool Servers | Add one MCP server: URL `http://freshservice-tools:8080/mcp`, Auth **Session**. Same screen as TitanMCP and QlikSense. | You |
| 3 | Open WebUI → Workspace → Tools | Add one tool, **Freshservice Key** (ID `freshservice_key`). It has no functions; it only provides each user's private key box. Access: read for the group that uses Freshservice. | You |
| 4 | Open WebUI, each user | Paste their own Freshservice key once: chat → **Controls → Valves → Tools → Freshservice Key**. | Each user |
| 5 | Open WebUI admin (optional cleanup) | Turn **Direct Integrations** and the group's **Direct Tool Servers** back off. No longer needed. | You |

**Not changed:** nginx, ports, certificates, DNS, the Unraid server.

---

## How a call works (example: Sam)

1. Sam asks something in chat. Open WebUI calls our server and, because Auth is **Session**, attaches Sam's login pass.
2. Our server hands that pass back to Open WebUI: `GET http://open-webui:8080/api/v1/tools/id/freshservice_key/valves/user`.
3. Open WebUI sees it's Sam's pass and returns **Sam's key only**.
4. Our server calls Freshservice with Sam's key. Freshservice answers as Sam, with Sam's permissions.
5. If Sam's box is empty, Sam gets a message saying where to paste the key.

Our server only talks to Open WebUI and Freshservice.

---

## Verified facts (Open WebUI v0.11.4 source)

| Fact | Where |
|---|---|
| Auth = Session sends the calling user's own Open WebUI token to the tool server, for MCP and OpenAPI alike. | `utils/tools.py`, `build_tool_server_headers()` |
| `GET /api/v1/tools/id/{id}/valves/user` returns only the calling user's own values, decided by the token. | `routers/tools.py`, `get_tools_user_valves_by_id` |
| The value is stored on that user's own row: `user.settings → tools.valves.freshservice_key`. | `models/tools.py` |
| Users set it under chat **Controls → Valves → Tools**; needs group permissions *Allow Chat Controls* and *Allow Chat Valves* (on by default). | `chat/Controls/Valves.svelte`, `Controls.svelte` |
| A tool with no functions is accepted (it just has an empty tool list). | `routers/tools.py` create → `get_tool_specs()` |
| `"type": "password"` masks the field. | Open WebUI docs; issue #20852 (completed) |
| A Freshservice API key carries that agent's own permissions. | Freshservice docs |

---

## The five charter questions

### 1. What is the one thing this must do?

Each Open WebUI user's Freshservice tool calls run with **the API key that user typed in themselves**, so Freshservice applies that user's own permissions. Nobody else ever handles their key. **Per-user keys are the only acceptable solution.**

### 2. What would be wrong if we shipped "working" software without it?

- Any fallback to a shared key. A user with no key gets "add your key" — never someone else's access.
- A call running with another user's key.
- Needing anything outside the change list above to make it work.

### 3. What is explicitly off-limits?

- nginx changes, published ports, certificates, DNS.
- A shared or service key on the Rosie container. It must not have `FRESHSERVICE_APIKEY`, and refuses to start if it's set.
- Anyone collecting or entering other users' keys.
- Logging a key or a login pass.
- Any write tool except `update_ticket_task_status` (sets a task to open or completed, nothing else, per-user mode only) and `complete_term_tasks` (added by PROJECT-term-tasks-skill.md, approved 2026-10-02; per-user mode only).
- Changing how the Unraid server behaves. Its tool list stays at the same 22 read tools.

### 4. Deployment target and backup

- **Code:** this repo, branch `feat/owui-per-user-keys`. Branch-only image build (`sha-…` tag); `:latest` (Unraid's image) is only rebuilt when merged to `main`, after verification.
- **Runs on:** the existing `freshservice-tools` Dockge stack on Rosie, Docker network only.
- **Backup:** code in git. Rosie's `/opt/stacks` is in git (`rosie-docker-stacks`); the stack's compose file gets a timestamped copy before it changes.
- **Every Rosie and Open WebUI step:** you do it, one step at a time, after I ask.

### 5. How will we verify it is done?

1. You paste your key in your box. Asking "what tasks are on ticket 47071?" returns the right tasks.
2. With your box emptied, the same question returns "add your Freshservice key" and no Freshservice call is made.
3. A second user's results match **their** Freshservice view, not yours. Needs one volunteer.
4. `update_ticket_task_status` moves a task you choose from Open to Completed and back. Freshservice's activity log credits **the user whose key was used**.
5. Container logs contain no key and no login pass.
6. The Unraid server is unchanged: same 22 read tools, Claude.ai connector still works.

---

## Plan

| # | Step | Who |
|---|---|---|
| 1 | Code: per-user mode serves **MCP** and reads each user's key from Open WebUI using their login pass. Remove the browser-facing OpenAPI interface (no longer used). Keep: no shared key, key never logged, task-status tool, Unraid unchanged. Tests. | Me |
| 2 | Commit, push, branch-only build. | Me, after your OK |
| 3–7 | Change list items 1–5 above, one at a time. | You |
| 8 | Verify (list above). | You + one volunteer |
| 9 | Merge to `main` only after step 8 passes. | Me, after your OK |

## Defaults taken (change any in one line)

- **Key cache:** the server remembers a user's key for 5 minutes, in memory only, keyed by a hash of their login pass.
- **Task statuses allowed:** Open (`1`) and Completed (`3`) only.
