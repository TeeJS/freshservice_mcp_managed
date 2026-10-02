# PROJECT CHARTER — "Term" skill + ticket notes

**Status:** REVISION 2 — APPROVED 2026-10-02. Step 1 done on branch `feat/term-tasks` (not committed): note tool + term note, 37/37 tests pass; mutations caught (notes public by default; preview writing the note). **Changed after sign-off (T.J.'s expectation):** preview also shows the note it would write, e.g. `Preview: would add a private note to Ticket #INC-47071: No Titan account; No Qlik account.` Step 2 awaits your OK.
**Created:** 2026-10-02
**Revision 1 (approved 2026-10-02):** the term skill. Steps 1–5 done: tool `complete_term_tasks` (commit `3159a52`), Rosie on `sha-3159a52`, skill **Term Tasks** added and attached to ERP Admin. Preview test not yet run.
**Revision 2 (2026-10-02):** adds ticket notes. A general "add a note to a ticket" tool (private by default, public only when asked), and the term tool writes its own private note on the ticket: just "No <system> account", one line per system without an account (T.J., 2026-10-02).

---

## What you'll type, and what you'll get

**Term** (in ERP Admin): *"I got a term for Employee ID 11116 on Ticket 47071"*

```
Employee ID 11116 linked to:
* AD user CPERKINS (Carol Perkins), Manager: …
* SAP User: …

Task #TSK-5590 on Ticket #INC-47071 completed as no Titan account exists.   ← links
Task #TSK-5589 on Ticket #INC-47071 completed as no Qlik account exists.
Task #TSK-5588 left open: SAP account exists.
Private note added to Ticket #INC-47071.                                       ← new
```

The private note on the ticket says only:

```
No Titan account
No Qlik account
```

**Note** (any time): *"Add a note to ticket 47071: called Carol's manager, laptop pickup Friday"*
→ `Private note added to Ticket #INC-47071.` Say "public note" to make it visible to the requester.

AD is never touched.

---

## Every change to the work environment

This is the complete list. Nothing else changes.

| # | Where | Change | Who |
|---|---|---|---|
| 1 | This server's code | Turn on `create_ticket_note`, rewritten: plain text in (line breaks kept), **private unless asked**, the private flag always sent explicitly, clean result. `complete_term_tasks` also writes a private note on real runs. Per-user mode only; Unraid never gets either. | Me |
| 2 | Rosie, `freshservice-tools` stack | New image (same one-line image change). | You |
| 3 | Open WebUI → **ERP Admin** model | Add one line to the Freshservice section of its prompt: notes are private unless the user asks for public. I'll give you the exact line. | You |

**Not changed:** nginx, ports, certificates, DNS, AD, Titan Reporting, the Unraid server.

---

## Terms

| Term | Plain meaning |
|---|---|
| **Private note** | Visible to agents only. |
| **Public note** | Also visible to the ticket's requester, who may be emailed about it. |
| **Skill** | An Open WebUI Workspace item holding instructions; the model loads it only when needed, so it costs almost no context otherwise. |
| **Term ticket** | A Freshservice ticket with category **Human Resources**, subcategory **Separation** (verified on 47071 and 47146). |

---

## How it works

**Term (unchanged, plus the note):**
1. The model runs the existing employee-ID lookup and prints the bullets.
2. The model calls `complete_term_tasks` with the ticket, the employee ID, and which of SAP / Titan / Qlik had **no** row (unchanged).
3. The tool, in code: checks the ticket is HR / Separation; checks the employee ID is on the ticket (or that you confirmed it); finds the three tasks by exact name; completes only open tasks for systems with no account; then **adds one private note**: "No <system> account", one line per system without an account. Preview mode does none of the writing, the note included.

**General note:** `create_ticket_note(ticket, text, private=true)`. The text goes in as plain text; the tool escapes it and keeps line breaks.

---

## The five charter questions

### 1. What is the one thing this must do?

Term: complete the SAP, Titan and Qlik tasks **only** for systems the person has no account in, report exactly what was done with links, and record it in a private note on the ticket ("No <system> account" lines only). Notes: add a note as the person asking, **private unless they ask for public**.

### 2. What would be wrong if we shipped "working" software without it?

- A public note when nobody asked for one: the requester would see it.
- A note that doesn't match what actually happened.
- Completing a task for a system where the person **does** have an account; touching AD, any other task, or a non-Separation ticket.

### 3. What is explicitly off-limits?

- Relying on Freshservice's default for private/public: the flag is always sent.
- Notes on Unraid.
- Emailing anyone (no notify addresses).
- Any write tool beyond `update_ticket_task_status`, `complete_term_tasks` and `create_ticket_note`.
- Any change outside the list above.

### 4. Deployment target and backup

- **Code:** branch `feat/term-tasks`, branch-only build; `:latest` (Unraid's image) only on merge, after verification.
- **Rosie:** compose file gets a timestamped copy before the image line changes.
- **Open WebUI:** you add the prompt line, after I ask. The Term Tasks skill text doesn't change.

### 5. How will we verify it is done?

1. **Preview** a term on a real ticket: report only (including the note it *would* write), nothing completed, **no note written**.
2. A **real** term run on a ticket you choose: only the expected tasks completed, and **one private note** reading "No <system> account" per missing system, both credited to you in Freshservice.
3. A general note on a ticket you choose: appears **private**, credited to you.
4. A **public** note only on a test ticket you choose (it's visible to the requester).
5. Automated tests: private by default, public only when asked, flag always sent, text escaped, no note in preview, note text is exactly the "No <system> account" lines, plus all the existing term-tool checks.

---

## Plan

| # | Step | Who |
|---|---|---|
| 1 | Code the note tool and the term note; tests. | Me |
| 2 | Commit, push, branch-only build. | Me, after your OK |
| 3 | Rosie image change. | You |
| 4 | Add the ERP Admin prompt line. | You |
| 5 | Verify (list above). | You |
| 6 | Merge to `main`. | Me, after your OK |

## Defaults taken (change any in one line)

- **Term note** is written only when at least one of SAP / Titan / Qlik has no account; if the person has all three, no note.
- **Term note** is always private, and says only "No <system> account" per missing system, whatever happened to the task.
- **Notes are plain text**: no formatting from the model, only line breaks.
- **Tasks with an account that's disabled/locked** still count as "has an account" (unchanged from revision 1).
- **Task link:** the ticket's Tasks tab (unchanged).
