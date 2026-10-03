# PROJECT CHARTER — Search tickets by text, on the server

**Status:** APPROVED 2026-10-02. Step 1 (code, tests, README) done on the branch, 59/59 tests pass. **Added with your OK (2026-10-02):** `get_requested_items` enabled as a read tool (it was disabled; the OWUI filter-list entry needed it). Read tools 22 → 24.
**Created:** 2026-10-02
**Branch:** `feat/search-tickets` (never `main`)

---

## The problem, measured

Asked for the helpdesk tickets behind four Titan user changes, ERP Admin ran 9 date-window searches, then searched by the users' own email addresses — because that is all `filter_tickets` offers. Freshservice's API can filter by requester, group, status, source and dates, **not by text and not by category**, and category is blank on about half of the tickets that matter. A 9-day window is 234 tickets (8 pages of 30); the model cannot read that, and a local 27B model should not try.

How the tickets are really filed (verified on 500+ tickets, 2026-10-02):

| Event | Where the name is | Anything else reliable? |
|---|---|---|
| Termination | Subject of the HR system's email: "TRANSACTION APPROVED: The Terminate an Employee transaction for *Name Emp#*" | the requester is the HR system's notification address |
| Onboarding | Subject of the portal request: "*Site*: *Name*, *Title* - *date* - Requested by *HR person*"; **or** the body of a hiring manager's email ("our new hire starts today") | the portal form's fields (via `get_requested_items`) |
| Titan rights / access | Body of an email from a manager or the user ("post invoices in Titan"); subject often says nothing | category `Titan / User / Change` when someone set it |

Only a text match over **subject and body** finds all three. The API cannot do it, so the server will.

---

## What you'll get

One new read tool, `search_tickets`, in both key modes:

```
search_tickets(words="Jane Doe", created_after="2026-06-01", created_before="2026-06-20")
```

The server runs the date window through Freshservice's filter API 30 tickets at a time, keeps the tickets whose subject or plain-text body contains every word, and returns only those, as compact records:

```
{"matches": [ {id, type, subject, status, status_name, priority, requester_id, responder_id, group_id,
               workspace_id, category, sub_category, item_category, created_at, updated_at, due_by,
               description_preview}, ... ],
 "matched": 2, "scanned": 234, "pages": 8, "truncated": false,
 "oldest_scanned": "2026-06-01T13:02:11Z", "query": "created_at:>'2026-06-01' AND created_at:<'2026-06-20'"}
```

- One tool call instead of nine; the model reads 2–20 records instead of 8 pages.
- Optional narrowing passed straight to Freshservice: `requester_email`, `source`, `group_id`, `status`. Optional `category` / `sub_category` applied on the server (exact, case-insensitive).
- `"quoted words"` match as a phrase. Matching is case-insensitive, plain substring, whitespace collapsed. `words` may be empty when you only want the narrowing (for example every Onboarding ticket in a month).
- Caps: 20 pages per call by default (`max_pages`, at most 40) and 100 matches returned. If either cap is hit, `truncated` is `true` and `oldest_scanned` says how far the scan got, so the next call can continue from there.
- `fields="all"` returns the full records instead, like the other list tools.
- Runs under the asking user's own key in per-user mode, so nobody can find a ticket they could not open.

---

## Every change to the work environment

| # | Where | Change | Who |
|---|---|---|---|
| 1 | This server's code (branch `feat/search-tickets`) | The `search_tickets` tool, registered as a read tool in both modes; tests; README. Plus, with your OK: `get_requested_items` moved from disabled to the read tools. Nothing else in the server changes. | Me |
| 2 | Rosie, `freshservice-tools` stack | Image line → the branch build for verification (one line from me, timestamped copy first), back to `:latest` after the merge. | You |
| 3 | Open WebUI → Admin → External Tool Servers → Freshservice → Function Name Filter List | Add `search_tickets` (you already added `get_requested_items`). | You |
| 4 | Open WebUI → **Freshservice** skill | New section "Finding onboarding, termination and access tickets": the three patterns above and which call to make. Text from me. | You |

**Not changed:** `filter_tickets` and every other tool; keys, modes, auth; the write tools; the compact-output setting; Unraid (it gets the new read tool with the next `:latest` pull, nothing to configure).

---

## Terms

| Term | Plain meaning |
|---|---|
| **Window** | The `created_after` … `created_before` dates. Freshservice treats both ends as inclusive, give or take its server time zone. |
| **Page** | 30 tickets from Freshservice, one API call, about 0.4 s. |
| **Scan** | Reading pages one after another and testing each ticket against the words. |
| **Cap** | The most pages (or matches) one call will take before it stops and says `truncated: true`. |
| **Compact record** | The 17-field ticket summary searches already return in compact mode. |
| **Requested items** | The form answers behind a portal Service Request (new user's name, title, department, systems). `get_requested_items` returns them for one ticket. |

---

## Charter

### 1. The one thing this must do

Given words and a date window, return every ticket in the window whose subject or plain-text body contains those words, as compact records, in one call, without the model paging.

### 2. What would be wrong if it shipped "working" without that

- A match that only looks at the subject, so "Invoice Posting" (body: "post invoices in Titan") is missed.
- Full ticket records or HTML in the result, so one search costs what a page did before.
- A silent stop at the cap, so a missing ticket looks like "no such ticket".
- A narrowing parameter the server pretends to apply (for example `category`) but does not.
- Any change to `filter_tickets` or the other tools.

### 3. Off-limits

- Building an index, cache or database of tickets; the search hits Freshservice live each time.
- A new container or process.
- Scanning past the cap "just in case".
- Fuzzy or spelling-tolerant matching claims; it is a plain substring match.
- Any change outside the table above.

### 4. Deployment target and backup

- **Code:** branch `feat/search-tickets`, branch-only build (`sha-…`). Merged to `main` only after verification and your OK; the merge rebuilds `:latest`.
- **Rosie:** compose file gets a timestamped copy before its image line changes, both times.
- **Open WebUI:** text only (filter list entry, skill section); previous skill text is in the repo.

### 5. How we verify it is done

1. **Automated tests** against a fake Freshservice with three pages (70 tickets): match in subject, match in body only, phrase match, case-insensitive match, no `description` HTML in any result; `requester_email`, `source`, `group_id`, `status` appear in the outbound query (asserted on the URL); `category` filtering; page cap → `truncated: true` with `oldest_scanned`; match cap; empty `words` with narrowing; bad dates and `created_before` earlier than `created_after` refused with no Freshservice call; `fields="all"`; identical behaviour in `full` output mode; allowlist counts updated; all existing tests pass.
2. **Rosie, new ERP Admin chat:** "find the onboarding ticket for <the June new hire> in June" → 44369 (and her 44418 email) within 3 tool calls. "Was there a termination ticket for <a name with none> in June?" → "none", not a guess. The original four-item Titan question → an answer with at most 6 tool calls and no searches by the users' own email.
3. **Term skill:** one term preview still works.
4. **Unraid, after the merge:** through the claude.ai connector, `search_tickets("<that surname>", "2026-06-09", "2026-06-20")` returns 44369 and 44418.

---

## Plan

| # | Step | Who |
|---|---|---|
| 1 | Code, tests, README on the branch. | Me |
| 2 | Commit, push, branch-only build. | Me, after your OK |
| 3 | Rosie: timestamped copy, image line → branch build, `up -d`, healthz. One line from me. | You |
| 4 | OWUI: add `search_tickets` to the filter list; paste the skill section. | You |
| 5 | Verification 2 and 3. | You |
| 6 | Merge to `main`. | Me, when you say |
| 7 | Rosie back to `:latest`; Unraid spot check (verification 4). | You / Me |

---

## Defaults taken (change any in one line)

- **Tool name:** `search_tickets`. **Required:** `words`, `created_after`. **Optional:** `created_before` (today), `requester_email`, `source`, `group_id`, `status`, `category`, `sub_category`, `max_pages` (20, max 40), `fields`.
- **Matched fields:** `subject` and `description_text` only. Not conversations, not attachments, not requested items.
- **Caps:** 20 pages, 100 matches; both reported through `truncated`.
- **Order:** newest first, as Freshservice returns them.
- **Shown in both modes**, including the claude.ai connector on Unraid.

---

## Known limits

- Cannot see text in attachments (46030's New Hire Form PDF), images (44365 "Request to Disable System Access" has no text), ticket replies, or the onboarding form fields — the last are one `get_requested_items` call away.
- Common surnames over-match (an IT person's signature, a cc); the model reads the previews and picks.
- A window longer than about three weeks needs more than one call.
- Each search costs one Freshservice API call per page; a full default scan is 20 calls, far inside the per-minute limit, and nothing is stored.

---

## Sign-off

Reply **"approved"**, or tell me what to change.
