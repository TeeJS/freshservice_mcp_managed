<!--
Open WebUI skill (Workspace > Skills > +). Attach it to the ERP Admin model.
  Skill Name:        Term Tasks
  Skill ID:          term-tasks
  Skill Description: Closes term-ticket tasks for systems with no account.
Paste everything below the line into Skill Instructions.
-->
---
# Term ticket tasks

Use this when the user says they got a term (termination / separation) for an employee ID on a Freshservice ticket, e.g. "I got a term for Employee ID 11116 on Ticket 47071".

## Procedure
1. Run the employee-ID lookup exactly as your instructions define it (the xfr.employee_users query) for the employee ID, and print the "Employee ID X linked to:" bullets exactly in that format.
2. If the lookup returns no rows, answer only "No user exists with employee ID X." and stop. Do not call any Freshservice tool.
3. From the rows, list which of SAP, Titan and Qlik have NO row. Never include AD: we don't administer AD.
4. Call `complete_term_tasks` with ticket_id = the ticket number, employee_id = the employee ID as typed, systems_without_account = that list (it may be empty). Add preview = true only if the user asked for a preview or dry run.
5. After the bullets, print every line in the tool's `lines`, exactly as given, one per line. Add nothing else.
6. If the result has `error`, show it and stop.
7. If the result has `needs_confirmation`, show its line and stop. Only if the user then confirms the employee ID is right for that ticket, call `complete_term_tasks` again with the same values and employee_confirmed = true.

## Rules
- Never use `update_ticket_task_status` for term tickets. `complete_term_tasks` does the task matching and the safety checks.
- An account counts as existing whatever its status (active, locked, disabled, deactivated), so its task stays open.
