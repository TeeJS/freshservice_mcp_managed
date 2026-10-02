# PROJECT CHARTER — Per-user Freshservice keys in Open WebUI

**Status:** APPROVED 2026-10-01 — steps 1–5 done on branch `feat/owui-per-user-keys`; step 6 (push + branch build) approved
**Created:** 2026-10-01
**Revision 2 (2026-10-01):** added the `update_ticket_task_status` write tool, limited to per-user mode; Unraid server explicitly unchanged.

**Progress (2026-10-01):** per-user mode, OpenAPI interface, `update_ticket_task_status`, key guard, refuse-to-start rules and README section written. `tests/test-per-user-mode.py`: 20/20 pass, including the Unraid checks (shared mode still registers exactly the 22 read tools and still uses `FRESHSERVICE_APIKEY`).

---

## Verified facts (research done, nothing changed)

| Fact | Where it was verified |
|---|---|
| Rosie OWUI is on v0.11.4, the latest release (2026-09-21). | GitHub releases |
| Users can add their own tool server under **Settings → Integrations → External Tool Servers**. Both switches are ON: **Direct Integrations** (global) and **Direct Tool Servers** (group permission). | OWUI v0.11.4 source; your screenshots 2026-10-01 |
| A user's own tool server must be **OpenAPI**. MCP is not offered there. | OWUI docs; `SettingsModal.svelte` |
| The **browser** makes the calls. With Auth = Bearer it sends `Authorization: Bearer <the key the user typed>` when loading the tool list and on every call. | `src/lib/apis/index.ts` (`getToolServersData`, `executeToolServer`) |
| The typed key is saved with the user's OWUI settings in OWUI's database, **not encrypted**. | `backend/open_webui/routers/users.py` |
| **mcpo can't pass a per-user key through.** v0.0.20 builds the forwarded headers and then never sends them. | mcpo `src/mcpo/utils/main.py` |
| Admin-level MCP connections can't take a per-user key. The feature is still open (open-webui#19313), and four attempts were closed unmerged. | GitHub |
| A Freshservice API key carries **that agent's own permissions**. | Freshservice docs |
| NW Pipe is on the **Pro** plan: 400 API calls/min for the whole account. **Invalid requests count against that limit.** | Header test 2026-10-01; Freshservice API docs |
| Freshservice's own MCP is ruled out: Pro gets about 6,000 actions a year (~16/day for the whole company). | Freshservice support docs |
| The code can **read** ticket tasks (`get_ticket_tasks`, `view_ticket_task`) but has **no tool to update** one. | `server.py` |
| Freshservice updates a ticket task with `PUT /api/v2/tickets/{ticket}/tasks/{task}`. Task `status` **1 = Open, 3 = Completed**. | API docs; live data from ticket #47071 |

**Why this is safe to expose internally:** the server holds no credential. A caller only gets what their own Freshservice key already allows them through the Freshservice API.

---

## The five charter questions

### 1. What is the one thing this must do?

Each OWUI user's Freshservice tool calls run with the API key **that user typed in themselves**, so Freshservice applies that user's own permissions. Nobody else ever handles their key.

### 2. What would be wrong if we shipped "working" software without it?

- **Any fallback to a shared key.** A user with no key set must get a clear "add your Freshservice key" error. It must never quietly use someone else's key.
- A user's call succeeding with **another user's** key.
- Blank or wrong keys reaching Freshservice over and over, using up the company-wide 400/min limit for every Freshservice integration.

### 3. What is explicitly off-limits as a workaround?

- T.J. or anyone else **collecting, storing or entering** other users' keys.
- A shared or service key on the Rosie instance. The container must not have `FRESHSERVICE_APIKEY` at all, and **must refuse to start** if it's set in this mode.
- Storing keys on our server.
- A key in a URL or query string. Logging the key or the `Authorization` header.
- A separate Python tool (User Valves) copy of the tools.
- mcpo in the path.
- Any write tool except **`update_ticket_task_status`**. That tool can set a ticket task's status and nothing else: no titles, assignees, notes or other fields.
- **Changing how the working Unraid server (Claude.ai, Authelia OAuth) behaves.** Its tool list stays at the same 22 read tools. `update_ticket_task_status` exists **only in per-user mode** and is never registered on the Unraid server.

### 4. Deployment target and backup location

- **Code:** this repo, on a branch. A one-off branch build makes a `sha-…` image for Rosie to test; `:latest` (Unraid's image) is only rebuilt when the branch is merged to `main`, after verification passes. The new mode stays off unless an env var turns it on.
- **Runs on:** a new Dockge stack on Rosie, behind nginx-proxy at `https://rosie.nwpipe.com/freshservice-tools/`. That's the same site as OWUI, so no cross-origin (CORS) setup is needed. The port is picked after a conflict check on Rosie, which needs your OK to look.
- **Backup:** code is covered by git. Rosie's `/opt/stacks` (including `nginx.conf`) is in git (`rosie-docker-stacks`, work account). We'll also take a timestamped copy of `nginx.conf` before editing it.
- **Every Rosie step:** you run it, or I ask first.

### 5. How will we verify it is done?

Not done until all of these pass:

1. A user adds the server with **their own key**. The tool list appears, and a ticket lookup returns data.
2. Permissions follow the key: a second agent's key returns what **their** Freshservice view shows, not yours. This needs one volunteer.
3. No key, a blank key or a junk key gets **401 from our server**. A blank or malformed key never reaches Freshservice. A wrong key gets one Freshservice check, and after that it's refused locally.
4. Every write tool except `update_ticket_task_status` is **missing** from `openapi.json` and **refused** if called by name.
5. `update_ticket_task_status` moves **a task you choose** from Open to Completed and back. Freshservice's activity log credits the change to **the user whose key was used**. Any status other than Open or Completed is refused, and no other field is sent.
6. The container has no `FRESHSERVICE_APIKEY`, and startup **refuses** if it's set in this mode.
7. After testing, the logs contain **no key**. We check by searching the logs for the key's first characters.
8. **The Unraid server is unchanged:** same image tag works with its current settings, its tool list is the same 22 read tools, and the Claude.ai connector still works.

---

## Known consequences, stated plainly

- **Keys sit unencrypted in OWUI's database** and its nightly backups (`/opt/ops/owui-backups/`). Anyone with access to either can read them. OWUI offers no encryption for this setting.
- Tools only run **while the user's OWUI tab is open**, because the browser makes the calls.
- Each user does a **one-time setup**: they paste the URL and their key. Only Freshservice agents have API keys.
- Everyone's calls share the company-wide 400/min limit.
- The model decides when to call tools. `update_ticket_task_status`'s description will tell it to change a task only when the user asks.

---

## Proposed plan

| # | Step | Needs from you |
|---|---|---|
| 1 | Per-request key: `get_auth_headers()` uses the key sent with the current request. In this mode no shared key exists. | none |
| 2 | OpenAPI interface: `openapi.json` plus one route per read tool, built from the same tool code. | none |
| 3 | `update_ticket_task_status(ticket_id, task_id, status)`: per-user mode only, sends `status` and nothing else. | none |
| 4 | Key guard: blank or malformed keys are refused locally. A new key is checked once with Freshservice, and the result is cached by its hash only, for a few minutes. | none |
| 5 | Mode switch, refuse-to-start rules, no key in logs, tests (including one proving the Unraid tool list is unchanged). | none |
| 6 | Push the branch, then run a one-off branch build: `sha-…` tag only, `:latest` (Unraid's image) untouched. Merge to `main` **only after step 8 passes**. | your OK to push (given 2026-10-01) |
| 7 | Rosie: Dockge stack + nginx location. | your OK to look/edit on Rosie |
| 8 | Verify (list above). | you + one volunteer agent + a task to test on |

## Defaults taken (change any in one line)

- **URL path:** `/freshservice-tools/`
- **Mode switch:** `FRESHSERVICE_KEY_MODE=per-user` (default `shared` = today's behaviour)
- **Key-check cache:** 10 minutes, in memory, stores only a SHA-256 hash of the key
- **Task statuses allowed:** Open (`1`) and Completed (`3`) only. These are the values confirmed in live data. In Progress (`2`) is left out until it's confirmed.
