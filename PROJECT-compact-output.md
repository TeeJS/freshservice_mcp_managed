# PROJECT CHARTER — Compact output for ticket searches

**Status:** VERIFIED 2026-10-02 on Rosie (image `sha-c01c313`, `FRESHSERVICE_OUTPUT: compact`): `/healthz` reports `"output":"compact"`; a 14-day search (532 tickets, 9 searches) stayed inside the context window; one ticket = 14,721 prompt tokens; a term preview ran unchanged. Merged to `main` 2026-10-02.
**Created:** 2026-10-02
**Branch:** `feat/compact-output` (never `main`)

---

## The problem, measured

A ticket search returns whole ticket records. Measured on the latest 100 tickets: a record is 41,500 characters in the middle, 78,000 at worst, and almost all of it is the HTML copy of the ticket's first message (38,400 characters in the middle). One page of 30 tickets is about 255,000 tokens. On Rosie that is 160 seconds of prompt processing for every later message in that chat, and one broad search today overflowed the model's 262,144-token window.

The same first message as plain text is 1,200 characters in the middle, 13,300 at worst. A compact record is about 450 characters.

---

## What you'll get

A search in ERP Admin ("find the tickets from the payroll system's notification address since September 25", "open tickets in the helpdesk group this month") comes back in seconds, with one short record per ticket:

```
{"id": 47146, "type": "Incident", "subject": "Printer offline in building 2", "status": 4, "status_name": "Resolved",
 "priority": 1, "requester_id": …, "responder_id": …, "group_id": …, "workspace_id": 2,
 "category": "Hardware", "sub_category": "Printer", "item_category": null,
 "created_at": "2026-10-02T10:06:40Z", "updated_at": "…", "due_by": "2026-10-16T00:00:00Z",
 "description_preview": "The printer on the second floor has been offline since this morning. …"}
```

- A page of 30 tickets is about 8,000 tokens instead of 255,000.
- Opening one ticket still gives the full plain-text message, custom fields and all; only the HTML copy is dropped.
- Whoever needs the old full records asks for them with `fields="all"`.

---

## Every change to the work environment

This is the complete list. Nothing else changes.

| # | Where | Change | Who |
|---|---|---|---|
| 1 | This server's code (branch `feat/compact-output`) | A new setting `FRESHSERVICE_OUTPUT`: `full` (the default, exactly today's behaviour) or `compact`. In compact mode, ticket lists return short records, and single tickets and conversations lose their HTML copies. Tests for all of it. | Me |
| 2 | Rosie, `freshservice-tools` stack | One new line in the compose file, `FRESHSERVICE_OUTPUT: compact`, and the image line moves to the branch build. One-line command from me; the compose file gets a timestamped copy first. | You |
| 3 | Open WebUI → **Freshservice** skill | One sentence added: lists are compact; use `get_ticket_by_id` for a ticket's full text. Text only. | You |

**Not changed:** the Unraid server (no setting there, so `full`, identical to today, even after the merge); the tool list and the Function Name Filter; keys, modes and auth; the write tools; nginx, ports, certificates, DNS.

---

## Terms

| Term | Plain meaning |
|---|---|
| **Token** | The unit a model reads in. Roughly 3.5 characters of this kind of text. |
| **Context window** | How many tokens a model can read at once. Rosie's current model: 262,144. Everything in a chat, including old tool results, is re-read on every message. |
| **HTML copy** | Freshservice keeps a ticket's first message twice: as HTML (`description`, with all the email formatting) and as plain text (`description_text`). Conversations have `body` and `body_text` the same way. |
| **Compact record** | A ticket reduced to the fields that identify it and say where it stands, plus a preview of its first message. |
| **Preview** | The first 500 characters of the plain-text first message, with runs of whitespace collapsed to one space. You chose 500 after seeing samples at 300, 500 and 750. |

---

## What compact mode does, tool by tool

| Tool | Compact mode |
|---|---|
| `filter_tickets`, `get_tickets` | Each ticket becomes: `id, type, subject, status, status_name, priority, requester_id, responder_id, group_id, workspace_id, category, sub_category, item_category, created_at, updated_at, due_by, description_preview`. The page count and `total` stay. A new optional argument `fields="all"` returns the full records instead. |
| `get_ticket_by_id` | The full record minus `description` (the HTML copy). `description_text`, custom fields, attachments and everything else stay. |
| `list_all_ticket_conversation` | Each conversation minus `body` (the HTML copy). `body_text` and everything else stay. |
| Everything else | Unchanged: tasks, notes, the term tool, changes, catalog, agents, requesters, solutions. |

In `full` mode every tool behaves exactly as today.

---

## Charter

### 1. The one thing this must do

With `FRESHSERVICE_OUTPUT=compact`, a ticket search returns records small enough that a page of 30 costs about 8,000 tokens, while the full ticket text stays one call away and nothing else about the server changes.

### 2. What would be wrong if it shipped "working" without that

- A list that still carries HTML or custom fields, so a page is still tens of thousands of tokens.
- A compact record missing something the existing skills rely on: `category` and `sub_category` (the term tool's Separation check), `subject`, `status`, who it is assigned to, the dates.
- A single ticket that lost more than the HTML copy, so a term run or a note can no longer see the plain text.
- Any change in `full` mode, which is what Unraid runs.
- Silent truncation anywhere other than the 500-character preview.

### 3. Off-limits

- Changing the default. `full` stays the default; only a stack that sets `compact` gets compact output.
- Removing fields from single tickets or conversations beyond the HTML copy.
- Touching the tool list, the Function Name Filter, auth, modes or the write tools.
- A new search tool. Compact mode makes the existing `filter_tickets` the slim search.
- Any change outside the table above.

### 4. Deployment target and backup

- **Code:** branch `feat/compact-output`, branch-only build (`sha-…` image). Merged to `main` only after verification and your OK. The merge rebuilds `:latest`; Unraid keeps `full` because it sets no `FRESHSERVICE_OUTPUT`.
- **Rosie:** the compose file gets a timestamped copy before its two lines change.
- **Open WebUI:** one sentence added to the Freshservice skill; the previous text is in the repo.

### 5. How we verify it is done

1. **Automated tests.** In compact mode a list record has exactly the fields above; the preview is at most 500 characters with whitespace collapsed; a page of 30 fake tickets with 30 KB HTML each comes out under 40 KB; `fields="all"` returns the full records; `get_ticket_by_id` loses only `description` and conversations only `body`; with no setting, every tool's output is byte-for-byte today's; an invalid setting refuses to start; `/healthz` reports the mode; all existing tests still pass, including the shared-mode checks.
2. **Rosie:** `/healthz` shows `"output": "compact"`. In a **new** ERP Admin chat, "find the tickets created in the last 14 days" answers in seconds with a list, and Open WebUI's token counter for that message stays under about 15,000. Then "show me ticket N" gives the full plain-text message.
3. **Term skill:** one term **preview** on Rosie still works in compact mode.
4. **Unraid:** nothing to do. The tests prove `full` is unchanged; after the merge, one spot check through the claude.ai connector that `get_ticket_by_id` still returns the HTML copy.

---

## Plan

| # | Step | Who |
|---|---|---|
| 1 | Code and tests on the branch. | Me |
| 2 | Commit, push, branch-only build. | Me, after your OK |
| 3 | Rosie: timestamped copy of the compose file, add the setting, change the image line, `docker compose pull` and `up -d`, check `/healthz`. One line from me. | You |
| 4 | Verification 2 and 3 in ERP Admin, plus the one sentence in the Freshservice skill. | You |
| 5 | Merge to `main`. | Me, when you say |
| 6 | Rosie to `:latest`, whenever you want. | You |

---

## Defaults taken (change any in one line)

- **Setting name:** `FRESHSERVICE_OUTPUT`, values `full` and `compact`.
- **Compact list fields:** the seventeen listed above, in that order.
- **Preview:** 500 characters of `description_text`, whitespace collapsed, no "…" added.
- **Full records on request:** `fields="all"` on `filter_tickets` and `get_tickets`; any other value is an error.
- **Single tickets and conversations** always drop the HTML copy in compact mode; there is no per-call switch for that.
- **Other list tools** (changes, catalog, solutions, requesters, agents) are left alone for now; they are hidden from ERP Admin anyway.
- **Page size** stays whatever Freshservice gives: 30 for `filter_tickets`; `get_tickets` keeps its `per_page`.

---

## Known limits

- A broad search still costs Freshservice API calls; the plan's 400 per minute is far away.
- A compact list cannot show custom fields; use `fields="all"` or open the ticket.
- The preview is the start of the message, which for forwarded emails is often the sender's signature and the "From:" header (seen in the samples).
- The 8,000-tokens figure is an estimate from the latest 100 tickets; a page of long human-written tickets can be more, because the preview is the only part that varies.

---

## Sign-off

Reply **"approved"**, or tell me what to change.
