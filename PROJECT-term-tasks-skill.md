# PROJECT CHARTER — "Term" skill: close tasks for systems the person has no account in

**Status:** APPROVED 2026-10-02. Step 1 done on branch `feat/term-tasks` (not committed): tool + 9 new tests, 32/32 pass; three mutations (complete regardless of accounts, drop the Separation check, drop the employee-ID check) each caught. Skill text: `owui/term-tasks-skill.md`. Step 2 awaits your OK.
**Created:** 2026-10-02

---

## What you'll type, and what you'll get

**You type** (in ERP Admin): *"I got a term for Employee ID 11116 on Ticket 47071"*

**You get:**

```
Employee ID 11116 linked to:
* AD user CPERKINS (Carol Perkins), Manager: …
* SAP User: …
(… exactly the same bullets as the employee-ID lookup today …)

Task #TSK-5590 on Ticket #INC-47071 completed as no Titan account exists.   ← both are links
Task #TSK-5589 on Ticket #INC-47071 completed as no Qlik account exists.
Task #TSK-5588 left open: SAP account exists.
```

AD is never touched: you don't administer it.

---

## Every change to the work environment

This is the complete list. Nothing else changes.

| # | Where | Change | Who |
|---|---|---|---|
| 1 | This server's code | One new tool, `complete_term_tasks`. It only works on HR **Separation** tickets, only on the three tasks named above, and only sets them to Completed. Per-user mode only, so it runs as you. | Me |
| 2 | Rosie, `freshservice-tools` stack | New image (same one-line image change as before). | You |
| 3 | Open WebUI → Workspace → **Skills** | Add one skill, **term-tasks**: the instructions, in about 30 lines. | You |
| 4 | Open WebUI → **ERP Admin** model | Attach the **term-tasks** skill. | You |

**Not changed:** nginx, ports, certificates, DNS, AD, Titan Reporting, the Unraid server (the new tool is never registered there).

---

## Terms

| Term | Plain meaning |
|---|---|
| **Skill** | An Open WebUI Workspace item holding instructions. A skill attached to a model costs only its name and one-line description in each chat; the model loads the full text (with its built-in `view_skill` tool) only when a term request comes in. That keeps ERP Admin's context small. Verified in Open WebUI v0.11.4's source (`utils/middleware.py`). |
| **Term ticket** | A Freshservice ticket with category **Human Resources**, subcategory **Separation**. Verified on tickets 47071 and 47146. |

---

## How it works

1. **The model** runs the existing employee-ID query (the same exact SQL as today) and prints the same bullets.
2. **The model** calls `complete_term_tasks` with the ticket number, the employee ID, and which of SAP / Titan / Qlik had **no** row. That's the only judgment left to the model.
3. **The tool**, in code, not the model:
   - checks the ticket is HR / Separation. If not, it changes nothing and says so.
   - checks the employee ID appears in the ticket's subject or text (47071 says "#11116", 47146 says "… Ketterman 37"). If not, it changes nothing and asks you to confirm.
   - finds each task by its **exact** name: `Termination of SAP Access`, `Terminate Titan Access`, `Terminate QlikSense Access`.
   - sets a task to **Completed** only if that system has no account and the task is open. A task that's already completed, missing, or listed twice is reported, not changed.
   - returns the short report lines, with links. The model prints them as-is.
4. Because the tool returns three short lines instead of all 21 tasks, this uses far less context than reading the ticket's tasks.

**Why a tool and not just instructions:** matching task names and deciding what to complete is done in tested code, every time the same way. Letting the model pick tasks out of 21 by itself is where a wrong task gets closed.

---

## The five charter questions

### 1. What is the one thing this must do?

Given an employee ID and a term ticket, complete the SAP, Titan and Qlik tasks **only** for systems where that person has no account, and report exactly what was done, with links.

### 2. What would be wrong if we shipped "working" software without it?

- Completing a task for a system where the person **does** have an account.
- Touching any task other than those three, or any ticket that isn't an HR Separation ticket.
- Touching AD.
- A report that says something was done when it wasn't, or the other way round.

### 3. What is explicitly off-limits?

- Changing anything but a task's status, and only to Completed.
- Any other write tool.
- The model choosing tasks by itself.
- Any change outside the list above.

### 4. Deployment target and backup

- **Code:** this repo, branch `feat/term-tasks`; a branch-only build first. `:latest` (Unraid's image) is rebuilt only when merged, after verification.
- **Rosie:** the `freshservice-tools` compose file gets a timestamped copy before the image line changes.
- **Every Rosie and Open WebUI step:** you do it, one at a time, after I ask.

### 5. How will we verify it is done?

1. **Preview first:** the tool has a preview mode that reports what it *would* do and changes nothing. Run it on a real term ticket and compare with Freshservice.
2. Then a real run on a term ticket you choose. Freshservice's Activity shows only the expected tasks completed, credited to you.
3. A ticket that isn't HR / Separation is refused with nothing changed.
4. An employee ID that isn't on the ticket is refused with nothing changed.
5. Automated tests cover all of the above against a fake Freshservice, plus "account exists → task left open", "already completed", "task missing" and "task listed twice".

---

## Plan

| # | Step | Who |
|---|---|---|
| 1 | Code `complete_term_tasks` + tests. | Me |
| 2 | Commit, push, branch-only build. | Me, after your OK |
| 3 | Rosie image change. | You |
| 4 | Add the **term-tasks** skill (I'll give you the exact text). | You |
| 5 | Attach it to ERP Admin. | You |
| 6 | Verify (list above): preview first, then one real run. | You |
| 7 | Merge to `main`. | Me, after your OK |

## Defaults taken (change any in one line)

- **A disabled, locked or deactivated account still counts as "has an account"**, so its task stays open for a person to handle.
- **Tasks left open are reported too**, one line each, so you know what's still yours.
- **Task link:** the ticket's Tasks tab (`…/a/tickets/<ticket>?current_tab=tasks`), the only task page I've seen. If clicking a task in Freshservice shows its own URL, paste it and I'll link straight to the task.
- **Labels:** `#TSK-<id>` and `#INC-<id>` / `#SR-<id>` (from the ticket's type), as Freshservice shows them.
