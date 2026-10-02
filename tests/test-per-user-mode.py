"""Per-user key mode tests. No Open WebUI or Freshservice connection needed.

Open WebUI and Freshservice are replaced by in-memory fakes, so every outbound
request can be inspected: which login token went to Open WebUI, which key went
to Freshservice, what body was sent, and whether anything was sent at all.
MCP requests go through the real app in-process; no port is opened.

Run from the repo root:  PYTHONPATH=src python tests/test-per-user-mode.py
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import sys

# Mode is read at import, so it must be set before the server is imported.
os.environ["FRESHSERVICE_KEY_MODE"] = "per-user"
os.environ["FRESHSERVICE_DOMAIN"] = "example.freshservice.com"
os.environ.pop("FRESHSERVICE_APIKEY", None)

import httpx

OWUI_URL = "http://owui.test"
KEY_A = "A" * 20
KEY_B = "B" * 20
KEY_REJECTED = "W" * 20  # Freshservice refuses it
# Freshservice doesn't document its key format, so a key with other
# characters must still be passed through for Freshservice to judge.
KEY_UNUSUAL = "Ab-cd_ef.gh+ij/kl=mn~op"

# What the fake Open WebUI returns for each login token.
OWUI_VALVES = {
    "tok-a": {"freshservice_api_key": KEY_A},
    "tok-b": {"freshservice_api_key": KEY_B},
    "tok-rejected": {"freshservice_api_key": KEY_REJECTED},
    "tok-empty": {},
    "tok-blank": {"freshservice_api_key": ""},
    "tok-unusual": {"freshservice_api_key": KEY_UNUSUAL},
}
TOKENS = list(OWUI_VALVES) + ["tok-noaccess", "tok-tool-missing"]


def basic(key):
    return "Basic " + base64.b64encode(f"{key}:X".encode()).decode()


def key_label(auth):
    return {basic(KEY_A): "key-a", basic(KEY_B): "key-b"}.get(auth, "other")


# --- Fake Open WebUI and Freshservice ----------------------------------------

outbound = []  # every request that left the server


def term_ticket(subject, tasks, category="Human Resources", sub_category="Separation", type_="Incident", text=""):
    return {"subject": subject, "description_text": text, "category": category,
            "sub_category": sub_category, "type": type_, "tasks": tasks}


def task(task_id, title, status=1):
    return {"id": task_id, "title": title, "status": status, "deleted": False}


def term_template(first_id):
    """The HR Separation template's tasks that matter here, plus AD."""
    return [
        task(first_id, "Active Directory Access"),
        task(first_id + 1, "Termination of SAP Access"),
        task(first_id + 2, "Terminate QlikSense Access"),
        task(first_id + 3, "Terminate Titan Access"),
        task(first_id + 4, "Ninja Remote Access"),
    ]


# Freshservice tickets the fake knows. Each term test uses its own ticket.
TICKETS = {
    60001: term_ticket("Urgent please turn off all access Jane Doe #11116", term_template(7001)),
    60002: term_ticket("New monitor", term_template(7101), category="Hardware", sub_category="Monitor"),
    60003: term_ticket("Please terminate Sam Roe", term_template(7201)),
    60004: term_ticket("Terminate an Employee transaction for Pat Poe 37",
                       [task(7301, "Termination of SAP Access"), task(7302, "Termination of SAP Access"),
                        task(7303, "Terminate Titan Access", status=3)], type_="Service Request"),
    60005: term_ticket("Terminate Lee Moe 4242", term_template(7401)),
    60006: term_ticket("Printer jam", [], category="Hardware", sub_category="Printer"),
    60007: {**term_ticket("Printer jam again", [], category="Hardware", sub_category="Printer"), "forces_public": True},
}


async def fake_upstreams(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(0.005)  # let concurrent calls interleave
    auth = request.headers.get("authorization", "")
    body = json.loads(request.content) if request.content else None
    host = request.url.host
    outbound.append({"host": host, "method": request.method, "path": request.url.path, "auth": auth, "body": body})

    if host == "owui.test":
        if request.url.path != "/api/v1/tools/id/freshservice_key/valves/user":
            return httpx.Response(404, json={"detail": "not found"})
        token = auth.removeprefix("Bearer ")
        if token == "tok-tool-missing":
            return httpx.Response(404, json={"detail": "We could not find what you're looking for :/"})
        if token not in OWUI_VALVES:
            return httpx.Response(401, json={"detail": "Not authenticated"})
        return httpx.Response(200, json=OWUI_VALVES[token])

    if host == "example.freshservice.com":
        if auth not in (basic(KEY_A), basic(KEY_B)):
            return httpx.Response(401, json={"message": "invalid credentials"})
        parts = request.url.path.strip("/").split("/")  # api v2 tickets <id> [tasks [<task id>]]
        ticket = TICKETS.get(int(parts[3])) if len(parts) > 3 and parts[3].isdigit() else None
        if request.method == "GET" and len(parts) == 4:
            if ticket is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"ticket": {k: v for k, v in ticket.items() if k != "tasks"}})
        if request.method == "GET" and parts[-1] == "tasks":
            # Report which key was used by label, never the key itself, so the
            # result can be asserted on without the key showing up in logs.
            tasks = ticket["tasks"] if ticket else []
            return httpx.Response(200, json={"tasks": tasks, "seen_key": key_label(auth)})
        if request.method == "POST" and len(parts) == 5 and parts[4] == "notes":
            if ticket is None:
                return httpx.Response(404, json={"message": "not found"})
            # Mirror the worst case: a note with no flag comes back public.
            saved_private = body.get("private", False) and not ticket.get("forces_public")
            return httpx.Response(201, json={"conversation": {
                "id": 9000 + len(outbound), "private": saved_private, "body": body["body"], "ticket_id": int(parts[3])}})
        if request.method == "PUT" and len(parts) == 6 and parts[4] == "tasks":
            task_id = int(parts[5])
            for task in (ticket or {}).get("tasks", []):
                if task["id"] == task_id:
                    task.update(body)
            return httpx.Response(200, json={"task": {"id": task_id, "status": body["status"]}})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.Response(599, json={"message": f"unexpected host {host}"})


_RealAsyncClient = httpx.AsyncClient


class FakeAsyncClient(_RealAsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(fake_upstreams))
        super().__init__(*args, **kwargs)


httpx.AsyncClient = FakeAsyncClient

# Capture every log line so we can prove no key or token is ever written.
log_lines = []


class _Capture(logging.Handler):
    def emit(self, record):
        log_lines.append(self.format(record))


logging.getLogger().addHandler(_Capture())
logging.getLogger().setLevel(logging.DEBUG)

from mcp import types  # noqa: E402

from freshservice_mcp import per_user, server  # noqa: E402

EXPECTED_TOOLS = server.READONLY_TOOLS | {"update_ticket_task_status", "complete_term_tasks", "create_ticket_note"}
APP = server.build_per_user_app(OWUI_URL)


# --- A minimal MCP client over the in-process app -----------------------------

def _sse_json(response):
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return response.json()


async def mcp_request(client, token, method, params=None):
    """Open a session as the caller with ``token``, then send one request."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    init = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": types.LATEST_PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    })
    assert init.status_code == 200, (init.status_code, init.text[:300])
    headers["mcp-session-id"] = init.headers["mcp-session-id"]
    headers["mcp-protocol-version"] = _sse_json(init)["result"]["protocolVersion"]
    note = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202, note.status_code
    r = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return _sse_json(r)["result"]


async def call_tool(client, token, name, arguments):
    result = await mcp_request(client, token, "tools/call", {"name": name, "arguments": arguments})
    text = "".join(c.get("text", "") for c in result.get("content", []))
    return result.get("isError", False), text


def since(mark, host=None, method=None):
    return [o for o in outbound[mark:] if (host is None or o["host"] == host) and (method is None or o["method"] == method)]


FS = "example.freshservice.com"


# --- MCP tests (one session manager for all of them) --------------------------

async def mcp_tests(results):
    async def check(name, coro):
        try:
            await coro
            results.append((name, None))
        except Exception as exc:
            results.append((name, exc))

    transport = httpx.ASGITransport(app=APP)
    async with server.mcp.session_manager.run():
        async with _RealAsyncClient(transport=transport, base_url="http://freshservice-tools:8080", timeout=20) as client:

            async def healthz_needs_no_token():
                r = await client.get("/healthz")
                assert r.status_code == 200, r.text
                assert r.json() == {"status": "ok", "mode": "per-user", "tools": 25}, r.json()

            async def tool_list_is_exactly_the_per_user_tools():
                result = await mcp_request(client, None, "tools/list")
                names = {t["name"] for t in result["tools"]}
                assert names == EXPECTED_TOOLS, names ^ EXPECTED_TOOLS
                for disabled in ["create_ticket", "update_ticket", "delete_ticket", "update_change_task"]:
                    assert disabled not in names, disabled

            async def each_call_uses_the_callers_own_key():
                for token, key in [("tok-a", KEY_A), ("tok-b", KEY_B), ("tok-a", KEY_A)]:
                    mark = len(outbound)
                    is_error, text = await call_tool(client, token, "get_ticket_tasks", {"ticket_id": 47071})
                    assert not is_error, text
                    assert json.loads(text)["seen_key"] == key_label(basic(key)), text
                    fs = since(mark, FS)
                    assert fs and all(o["auth"] == basic(key) for o in fs), fs
                    owui = since(mark, "owui.test")
                    assert all(o["auth"] == f"Bearer {token}" for o in owui), owui

            async def concurrent_callers_never_see_each_others_key():
                pairs = [("tok-a", KEY_A), ("tok-b", KEY_B)] * 8
                outcomes = await asyncio.gather(*[
                    call_tool(client, token, "get_ticket_tasks", {"ticket_id": 47071}) for token, _ in pairs
                ])
                for (token, key), (is_error, text) in zip(pairs, outcomes):
                    assert not is_error, text
                    assert json.loads(text)["seen_key"] == key_label(basic(key)), f"{token} ran with another caller's key"

            async def key_is_cached_not_fetched_every_call():
                await call_tool(client, "tok-a", "get_ticket_tasks", {"ticket_id": 1})
                mark = len(outbound)
                await call_tool(client, "tok-a", "get_ticket_tasks", {"ticket_id": 1})
                assert not since(mark, "owui.test"), "key was fetched again inside the cache window"

            async def refused(token, expect_text):
                mark = len(outbound)
                is_error, text = await call_tool(client, token, "get_ticket_tasks", {"ticket_id": 1})
                assert is_error, text
                assert expect_text in text, text
                assert not since(mark, FS), f"Freshservice was called for {token}"

            async def no_login_token_is_refused():
                mark = len(outbound)
                await refused(None, "Auth must be set to Session")
                assert not since(mark, "owui.test"), "Open WebUI was asked without a token"

            async def missing_key_says_where_to_add_it():
                await refused("tok-empty", "Controls > Valves > Tools > Freshservice Key")
                await refused("tok-blank", "Controls > Valves > Tools > Freshservice Key")

            async def unusual_key_is_left_for_freshservice_to_judge():
                mark = len(outbound)
                await call_tool(client, "tok-unusual", "get_ticket_tasks", {"ticket_id": 1})
                fs = since(mark, FS)
                assert fs, "a key with unusual characters was refused locally"
                assert all(o["auth"] == basic(KEY_UNUSUAL) for o in fs), fs

            async def no_access_or_expired_login_is_refused():
                await refused("tok-noaccess", "wouldn't share")

            async def missing_key_tool_is_reported():
                await refused("tok-tool-missing", "Freshservice Key tool isn't installed")

            async def empty_key_is_not_cached():
                # A user who adds their key right after the refusal must not wait.
                await refused("tok-empty", "Add your Freshservice API key")
                OWUI_VALVES["tok-empty"] = {"freshservice_api_key": KEY_A}
                try:
                    is_error, text = await call_tool(client, "tok-empty", "get_ticket_tasks", {"ticket_id": 1})
                    assert not is_error, text
                finally:
                    OWUI_VALVES["tok-empty"] = {}

            async def rejected_key_returns_freshservice_error():
                is_error, text = await call_tool(client, "tok-rejected", "get_ticket_tasks", {"ticket_id": 1})
                assert "Failed to fetch ticket tasks" in text, text

            async def unknown_and_disabled_tools_never_reach_freshservice():
                for name in ["create_ticket", "update_ticket", "delete_ticket", "update_change_task"]:
                    mark = len(outbound)
                    is_error, text = await call_tool(client, "tok-a", name, {})
                    assert is_error, (name, text)
                    assert not since(mark, FS), f"{name} reached Freshservice"

            async def task_status_sends_only_the_status():
                for status, code in [("completed", 3), ("open", 1)]:
                    mark = len(outbound)
                    is_error, text = await call_tool(
                        client, "tok-a", "update_ticket_task_status",
                        {"ticket_id": 47071, "task_id": 5587, "status": status},
                    )
                    assert not is_error, text
                    puts = since(mark, FS, "PUT")
                    assert len(puts) == 1, puts
                    assert puts[0]["path"] == "/api/v2/tickets/47071/tasks/5587", puts[0]["path"]
                    assert puts[0]["body"] == {"status": code}, puts[0]["body"]
                    assert puts[0]["auth"] == basic(KEY_A)

            async def task_status_refuses_anything_but_open_or_completed():
                for status in ["in progress", "closed", 2, 3, ""]:
                    mark = len(outbound)
                    is_error, text = await call_tool(
                        client, "tok-a", "update_ticket_task_status",
                        {"ticket_id": 47071, "task_id": 5587, "status": status},
                    )
                    assert is_error, (status, text)
                    assert not since(mark, FS, "PUT"), f"status {status!r} reached Freshservice"

            # --- complete_term_tasks ---
            URL = "https://example.freshservice.com/a/tickets"

            async def term(**arguments):
                is_error, text = await call_tool(client, "tok-a", "complete_term_tasks", arguments)
                return is_error, (text if is_error else json.loads(text))

            def statuses(ticket_id):
                return {t["id"]: t["status"] for t in TICKETS[ticket_id]["tasks"]}

            async def term_preview_changes_nothing():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60001, employee_id="11116",
                                         systems_without_account=["Titan", "Qlik"], preview=True)
                assert not is_error, r
                assert not since(mark, FS, "PUT"), "preview changed a task"
                assert not since(mark, FS, "POST"), "preview added a note"
                assert r["completed_task_ids"] == [] and r["note_added"] is False, r
                assert r["lines"] == [
                    f"[Task #TSK-7002]({URL}/60001?current_tab=tasks) left open: SAP account exists.",
                    f"Preview: would complete [Task #TSK-7004]({URL}/60001?current_tab=tasks) on [Ticket #INC-60001]({URL}/60001) as no Titan account exists.",
                    f"Preview: would complete [Task #TSK-7003]({URL}/60001?current_tab=tasks) on [Ticket #INC-60001]({URL}/60001) as no Qlik account exists.",
                    f"Preview: would add a private note to [Ticket #INC-60001]({URL}/60001): No Titan account; No Qlik account.",
                ], r["lines"]

            async def term_completes_only_systems_without_an_account():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60001, employee_id="11116", systems_without_account=["Titan", "Qlik"])
                assert not is_error, r
                puts = since(mark, FS, "PUT")
                assert sorted(p["path"] for p in puts) == [
                    "/api/v2/tickets/60001/tasks/7003", "/api/v2/tickets/60001/tasks/7004"], puts
                assert all(p["body"] == {"status": 3} and p["auth"] == basic(KEY_A) for p in puts), puts
                assert r["lines"] == [
                    f"[Task #TSK-7002]({URL}/60001?current_tab=tasks) left open: SAP account exists.",
                    f"[Task #TSK-7004]({URL}/60001?current_tab=tasks) on [Ticket #INC-60001]({URL}/60001) completed as no Titan account exists.",
                    f"[Task #TSK-7003]({URL}/60001?current_tab=tasks) on [Ticket #INC-60001]({URL}/60001) completed as no Qlik account exists.",
                    f"Private note added to [Ticket #INC-60001]({URL}/60001).",
                ], r["lines"]
                notes = since(mark, FS, "POST")
                assert [(n["path"], n["body"]) for n in notes] == [
                    ("/api/v2/tickets/60001/notes", {"body": "No Titan account<br>No Qlik account", "private": True})], notes
                assert notes[0]["auth"] == basic(KEY_A)
                # AD, SAP and every other task untouched.
                assert statuses(60001) == {7001: 1, 7002: 1, 7003: 3, 7004: 3, 7005: 1}, statuses(60001)

            async def term_second_run_reports_already_completed():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60001, employee_id="11116", systems_without_account=["Titan", "Qlik"])
                assert not since(mark, FS, "PUT"), "a completed task was changed again"
                assert r["lines"][1].endswith("was already completed."), r["lines"]
                assert r["lines"][2].endswith("was already completed."), r["lines"]

            async def term_refuses_a_ticket_that_is_not_hr_separation():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60002, employee_id="11116", systems_without_account=["SAP"])
                assert "is not an HR Separation ticket" in r["error"], r
                assert not since(mark, FS, "PUT") and not since(mark, FS, "POST")
                assert statuses(60002) == {t: 1 for t in range(7101, 7106)}

            async def term_asks_when_the_employee_id_is_not_on_the_ticket():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60003, employee_id="11116", systems_without_account=["SAP"])
                assert r.get("needs_confirmation") is True, r
                assert "isn't on" in r["lines"][0], r
                assert not since(mark, FS, "PUT") and not since(mark, FS, "POST")
                # The user confirms: now it proceeds.
                is_error, r = await term(ticket_id=60003, employee_id="11116",
                                         systems_without_account=["SAP"], employee_confirmed=True)
                assert r["completed_task_ids"] == [7202], r
                assert since(mark, FS, "POST")[-1]["body"] == {"body": "No SAP account", "private": True}
                assert statuses(60003) == {7201: 1, 7202: 3, 7203: 1, 7204: 1, 7205: 1}, statuses(60003)

            async def term_matches_employee_id_ignoring_leading_zeros():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60005, employee_id="0004242", systems_without_account=[])
                assert "needs_confirmation" not in r, r
                assert [line.split(" left open: ")[1] for line in r["lines"]] == [
                    "SAP account exists.", "Titan account exists.", "Qlik account exists."], r["lines"]
                assert not since(mark, FS, "PUT")
                assert not since(mark, FS, "POST") and r["note_added"] is False, "a note was added though every account exists"

            async def term_reports_duplicate_missing_and_done_tasks_without_changes():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60004, employee_id="37",
                                         systems_without_account=["SAP", "Titan", "Qlik"])
                assert not since(mark, FS, "PUT"), "a duplicate, missing or completed task was changed"
                assert r["lines"] == [
                    f"More than one \"Termination of SAP Access\" task on [Ticket #SR-60004]({URL}/60004); nothing changed for SAP.",
                    f"[Task #TSK-7303]({URL}/60004?current_tab=tasks) on [Ticket #SR-60004]({URL}/60004) was already completed.",
                    f"No \"Terminate QlikSense Access\" task on [Ticket #SR-60004]({URL}/60004); nothing changed for Qlik.",
                    f"Private note added to [Ticket #SR-60004]({URL}/60004).",
                ], r["lines"]
                assert since(mark, FS, "POST")[-1]["body"] == {
                    "body": "No SAP account<br>No Titan account<br>No Qlik account", "private": True}

            async def term_never_accepts_ad():
                mark = len(outbound)
                is_error, text = await call_tool(client, "tok-a", "complete_term_tasks",
                                                 {"ticket_id": 60005, "employee_id": "4242",
                                                  "systems_without_account": ["AD"]})
                assert is_error, text
                assert not since(mark, FS), "a request with AD reached Freshservice"

            # --- create_ticket_note ---
            async def note(**arguments):
                is_error, text = await call_tool(client, "tok-a", "create_ticket_note", arguments)
                return is_error, (text if is_error else json.loads(text))

            async def note_is_private_by_default():
                mark = len(outbound)
                is_error, r = await note(ticket_id=60006, body="Called the manager, laptop pickup Friday")
                assert not is_error, r
                posts = since(mark, FS, "POST")
                assert [p["body"] for p in posts] == [
                    {"body": "Called the manager, laptop pickup Friday", "private": True}], posts
                assert posts[0]["auth"] == basic(KEY_A)
                assert r["line"] == f"Private note added to [Ticket #INC-60006]({URL}/60006).", r

            async def note_is_public_only_when_asked():
                mark = len(outbound)
                is_error, r = await note(ticket_id=60006, body="Your laptop is ready", private=False)
                assert [p["body"]["private"] for p in since(mark, FS, "POST")] == [False]
                assert r["line"] == f"Public note added to [Ticket #INC-60006]({URL}/60006).", r

            async def note_text_is_escaped_and_keeps_line_breaks():
                mark = len(outbound)
                await note(ticket_id=60006, body="<b>bold?</b> a & b\nsecond line\r\nthird")
                assert since(mark, FS, "POST")[0]["body"]["body"] == (
                    "&lt;b&gt;bold?&lt;/b&gt; a &amp; b<br>second line<br>third")

            async def note_refuses_empty_text_and_missing_tickets():
                mark = len(outbound)
                is_error, r = await note(ticket_id=60006, body="   ")
                assert "empty" in r["error"], r
                is_error, r = await note(ticket_id=99999, body="hello")
                assert "Couldn't read ticket 99999" in r["error"], r
                assert not since(mark, FS, "POST"), "a note was posted anyway"

            async def note_reports_what_freshservice_actually_saved():
                is_error, r = await note(ticket_id=60007, body="should be private")
                assert "saved it as PUBLIC, not as asked" in r["line"], r

            async def term_rejects_a_non_numeric_employee_id():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60005, employee_id="abc", systems_without_account=["SAP"])
                assert "must be a number" in r["error"], r
                assert not since(mark, FS), "Freshservice was called for a bad employee ID"

            for fn in [
                healthz_needs_no_token,
                tool_list_is_exactly_the_per_user_tools,
                each_call_uses_the_callers_own_key,
                concurrent_callers_never_see_each_others_key,
                key_is_cached_not_fetched_every_call,
                no_login_token_is_refused,
                missing_key_says_where_to_add_it,
                unusual_key_is_left_for_freshservice_to_judge,
                no_access_or_expired_login_is_refused,
                missing_key_tool_is_reported,
                empty_key_is_not_cached,
                rejected_key_returns_freshservice_error,
                unknown_and_disabled_tools_never_reach_freshservice,
                task_status_sends_only_the_status,
                task_status_refuses_anything_but_open_or_completed,
                term_preview_changes_nothing,
                term_completes_only_systems_without_an_account,
                term_second_run_reports_already_completed,
                term_refuses_a_ticket_that_is_not_hr_separation,
                term_asks_when_the_employee_id_is_not_on_the_ticket,
                term_matches_employee_id_ignoring_leading_zeros,
                term_reports_duplicate_missing_and_done_tasks_without_changes,
                term_never_accepts_ad,
                term_rejects_a_non_numeric_employee_id,
                note_is_private_by_default,
                note_is_public_only_when_asked,
                note_text_is_escaped_and_keeps_line_breaks,
                note_refuses_empty_text_and_missing_tickets,
                note_reports_what_freshservice_actually_saved,
            ]:
                await asyncio.wait_for(check(fn.__name__, fn()), timeout=60)


# --- Plain tests --------------------------------------------------------------

def test_no_fallback_to_a_shared_key():
    saved = server.FRESHSERVICE_APIKEY
    server.FRESHSERVICE_APIKEY = "S" * 20  # as if one had been set anyway
    try:
        server.get_auth_headers()
    except RuntimeError:
        pass
    else:
        raise AssertionError("get_auth_headers() fell back to the shared key")
    finally:
        server.FRESHSERVICE_APIKEY = saved


def test_logs_never_contain_a_key_or_token():
    secrets = [KEY_A, KEY_B, KEY_REJECTED, KEY_UNUSUAL] + [t for t in TOKENS]
    secrets += [basic(k).split(" ", 1)[1] for k in (KEY_A, KEY_B, KEY_REJECTED, KEY_UNUSUAL)]
    leaked = [line for line in log_lines for s in secrets if s in line]
    assert not leaked, leaked[:3]


def _python(code, env_overrides, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_overrides)
    env["PYTHONPATH"] = os.path.join(os.getcwd(), "src")
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)


def test_shared_mode_tool_list_is_unchanged():
    """The Unraid deployment: no FRESHSERVICE_KEY_MODE, a shared key, same 22 tools."""
    r = _python(
        "import json; from freshservice_mcp.server import mcp, KEY_MODE, READONLY_TOOLS;"
        "print(json.dumps([KEY_MODE, sorted(t.name for t in mcp._tool_manager.list_tools()), sorted(READONLY_TOOLS)]))",
        {"FRESHSERVICE_APIKEY": "S" * 20},
        drop=("FRESHSERVICE_KEY_MODE",),
    )
    assert r.returncode == 0, r.stderr[-500:]
    mode, registered, readonly = json.loads(r.stdout.strip().splitlines()[-1])
    assert mode == "shared", mode
    assert registered == readonly, set(registered) ^ set(readonly)
    assert len(registered) == 22, len(registered)
    assert "update_ticket_task_status" not in registered
    assert "complete_term_tasks" not in registered
    assert "create_ticket_note" not in registered


def test_shared_mode_still_uses_the_shared_key():
    r = _python(
        "from freshservice_mcp.server import get_auth_headers; print(get_auth_headers()['Authorization'])",
        {"FRESHSERVICE_APIKEY": "S" * 20},
        drop=("FRESHSERVICE_KEY_MODE",),
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip().splitlines()[-1] == basic("S" * 20)


def test_per_user_mode_refuses_to_start_with_a_shared_key():
    r = _python("from freshservice_mcp.server import main; main()",
                {"FRESHSERVICE_APIKEY": "S" * 20, "OWUI_URL": OWUI_URL})
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "FRESHSERVICE_APIKEY is set" in r.stderr, r.stderr[-500:]


def test_per_user_mode_refuses_to_start_without_a_domain():
    r = _python("from freshservice_mcp.server import main; main()", {"OWUI_URL": OWUI_URL}, drop=("FRESHSERVICE_DOMAIN",))
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "FRESHSERVICE_DOMAIN is required" in r.stderr, r.stderr[-500:]


def test_per_user_mode_refuses_to_start_without_owui_url():
    r = _python("from freshservice_mcp.server import main; main()", {}, drop=("OWUI_URL",))
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "OWUI_URL is required" in r.stderr, r.stderr[-500:]


def test_unknown_mode_refuses_to_start():
    r = _python("from freshservice_mcp.server import main; main()", {"FRESHSERVICE_KEY_MODE": "peruser"})
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "is not one of" in r.stderr, r.stderr[-500:]


if __name__ == "__main__":
    results = []
    asyncio.run(mcp_tests(results))
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                results.append((name, None))
            except Exception as exc:
                results.append((name, exc))

    failures = 0
    for name, exc in results:
        if exc is None:
            print(f"PASS: {name}")
        else:
            failures += 1
            print(f"FAIL: {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(results) - failures}/{len(results)} passed")
    sys.exit(1 if failures else 0)
