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
    60008: {**term_ticket("Laptop fan noise", [], category="Hardware", sub_category="Laptop"),
            "description": "<div><b>Hello</b><br>fan noise</div>", "description_text": "Hello fan noise",
            "custom_fields": {"lf_location": 1}, "attachments": []},
    # The status and category tool: each test that changes a ticket uses its own.
    60009: {**term_ticket("Monitor flicker", [], category="Hardware", sub_category="Monitor"), "status": 2, "item_category": "Dell"},
    60010: {**term_ticket("Printer driver", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None},
    60011: {**term_ticket("Cannot save", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "rejects_update": True},
    60012: {**term_ticket("Ignores status", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "ignores_status": True},
    60013: {**term_ticket("No category yet", [], category=None, sub_category=None), "status": 2, "item_category": None},
    # Assignment tests. "Me" depends on the key: see AGENT_IDS.
    60014: {**term_ticket("Unassigned", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None, "responder_id": None},
    60015: {**term_ticket("Someone else's", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None, "responder_id": 777},
    60016: {**term_ticket("Already mine", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None, "responder_id": 5001},
    60017: {**term_ticket("Agent lookup fails", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None, "responder_id": None},
    60018: {**term_ticket("Resolution notes", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None, "resolution_notes": None},
    60019: {**term_ticket("Resolution ignored", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "resolution_notes": "", "ignores_resolution": True},
    # Location: lf_location holds a Freshservice location id.
    60020: {**term_ticket("No location", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "custom_fields": {"lf_location": None}},
    60021: {**term_ticket("Has a location", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "custom_fields": {"lf_location": 33000357140}},
    60022: {**term_ticket("Location ignored", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "custom_fields": {"lf_location": None}, "ignores_location": True},
    60023: {**term_ticket("Location lookup", [], category="Hardware", sub_category="Printer"), "status": 2, "item_category": None,
            "custom_fields": {"lf_location": None}},
}
# 130 filler sites push the real ones onto page 2 of the 100-per-page list.
LOCATIONS = [{"id": 900 + i, "name": f"Site {i}"} for i in range(130)] + [
    {"id": 33000357140, "name": "Portland"}, {"id": 4242, "name": "Seattle"}, {"id": 4243, "name": "Seattle Annex"},
    {"id": 5001, "name": "Twin"}, {"id": 5002, "name": "twin"}]
locations_forbidden = {"on": False}
AGENT_IDS = {KEY_A: 5001, KEY_B: 5002}  # the Freshservice agent each key belongs to
agent_lookup_fails = {"on": False}      # when on, /agents/me answers 403


def list_ticket(i):
    """A ticket as Freshservice returns it in a list: the HTML copy of the first message dwarfs the rest."""
    text = f"Line one of ticket {i}.\n\n   Line   two, with   spaces.\r\n" * 40  # about 1,900 characters
    return {
        "id": 70000 + i, "type": "Incident" if i % 2 else "Service Request", "subject": f"Subject {i}",
        "status": 2, "status_name": "Open", "priority": 1, "requester_id": 11, "responder_id": 22,
        "group_id": 33, "workspace_id": 2, "category": "Hardware", "sub_category": "Monitor",
        "item_category": None, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
        "due_by": "2026-10-09T00:00:00Z", "fr_due_by": "2026-10-03T00:00:00Z", "is_escalated": False,
        "description": '<div style="font-size: 11px">' + "<span>word</span> " * 1500 + "</div>",  # about 30 KB
        "description_text": text,
        "custom_fields": {"a": None, "b": "x", "lf_location": 123}, "attachments": [], "cc_emails": ["x@example.com"],
    }


LIST_PAGE = [list_ticket(i) for i in range(30)]
CONVERSATIONS = {"conversations": [
    {"id": 1, "body": "<div>Hi<br>there</div>", "body_text": "Hi there", "private": True, "user_id": 5, "attachments": []},
    {"id": 2, "body": "<p>Fixed</p>", "body_text": "Fixed", "private": False, "user_id": 6, "attachments": []},
], "meta": {"count": 2, "has_more": False}}


def search_ticket(i):
    """Filler for the search fixture: 70 tickets, newest first, every one with an HTML description."""
    hours_back = 6 * i
    day, hour = divmod(hours_back, 24)
    created = f"2026-06-{18 - day:02d}T{23 - hour:02d}:00:00Z"
    return {
        "id": 80000 + i, "type": "Incident", "subject": f"Printer jam {i}", "status": 2, "status_name": "Open",
        "priority": 1, "requester_id": 100 + i, "responder_id": 22, "group_id": 33, "workspace_id": 2,
        "category": None, "sub_category": None, "item_category": None, "source": 1,
        "created_at": created, "updated_at": created, "due_by": "2026-06-30T00:00:00Z",
        "description": "<div>" + "<span>printer</span> " * 300 + "</div>",
        "description_text": f"Printer {i} jammed again. Please fix.",
        "custom_fields": {"lf_location": 1}, "attachments": [],
    }


SEARCH_TICKETS = [search_ticket(i) for i in range(70)]
SEARCH_TICKETS[2].update({  # page 1: the name only in the subject, no text at all (a portal request)
    "subject": "Plant A: Jane Doe, Will Call - Mon, Jun 15, 2026 - Requested by Pat Example", "type": "Service Request",
    "source": 2, "category": "Human Resources", "sub_category": "Onboarding", "description": "", "description_text": ""})
SEARCH_TICKETS[35].update({  # page 2: the system only in the body, subject says nothing
    "subject": "Invoice Posting",
    "description_text": "Hello,\n\nWe need Erin to have access to the ability to   post invoices in Titan.\nThanks"})
SEARCH_TICKETS[40].update({  # page 2: name and system in the body
    "subject": "Log in to Titan", "description_text": "I'm a new employee and need access to Titan. Thank you, Jane Doe"})
SEARCH_TICKETS[65].update({  # page 3: a Dayforce termination
    "subject": "TRANSACTION APPROVED: The Terminate an Employee transaction for John Doe 1234",
    "requester_id": 11, "category": "Human Resources", "sub_category": "Separation", "item_category": "Full-Time Employee",
    "description_text": "The Terminate an Employee transaction for John Doe 1234 submitted by Sam Roe 5678 was approved."})
SEARCH_PAGES = [SEARCH_TICKETS[0:30], SEARCH_TICKETS[30:60], SEARCH_TICKETS[60:70]]
SEARCH_WINDOW = "created_at:>'2026-06-01'"      # queries starting like this get the fixture
BROKEN_WINDOW = "created_at:>'2026-02-01'"      # page 1 works, page 2 is a Freshservice error
search_queries = []  # every filter query the fixture served, unquoted, in order


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
        if request.method == "GET" and request.url.path == "/api/v2/agents/me":
            if agent_lookup_fails["on"]:
                return httpx.Response(403, json={"code": "access_denied"})
            key = KEY_A if auth == basic(KEY_A) else KEY_B
            return httpx.Response(200, json={"agent": {"id": AGENT_IDS[key], "first_name": "Test"}})
        if request.method == "GET" and request.url.path == "/api/v2/locations":
            if locations_forbidden["on"]:
                return httpx.Response(403, json={"code": "access_denied"})
            page, per_page = int(request.url.params.get("page", "1")), int(request.url.params.get("per_page", "30"))
            return httpx.Response(200, json={"locations": LOCATIONS[(page - 1) * per_page: page * per_page]})
        if request.method == "GET" and request.url.path == "/api/v2/tickets/filter":
            query = request.url.params.get("query", "").strip('"')
            page = int(request.url.params.get("page", "1"))
            if query.startswith(SEARCH_WINDOW):
                search_queries.append(query)
                return httpx.Response(200, json={"tickets": SEARCH_PAGES[page - 1] if page <= 3 else [], "total": 70})
            if query.startswith(BROKEN_WINDOW):
                if page == 1:
                    return httpx.Response(200, json={"tickets": SEARCH_PAGES[0], "total": 70})
                return httpx.Response(500, json={"description": "boom"})
            return httpx.Response(200, json={"tickets": LIST_PAGE, "total": len(LIST_PAGE)})
        if request.method == "GET" and request.url.path == "/api/v2/tickets":
            return httpx.Response(200, json=LIST_PAGE,
                                  headers={"Link": '<https://example.freshservice.com/api/v2/tickets?page=2>; rel="next"'})
        parts = request.url.path.strip("/").split("/")  # api v2 tickets <id> [tasks [<task id>]]
        ticket = TICKETS.get(int(parts[3])) if len(parts) > 3 and parts[3].isdigit() else None
        if request.method == "GET" and len(parts) == 5 and parts[4] == "conversations":
            return httpx.Response(200, json=CONVERSATIONS)
        if request.method == "GET" and len(parts) == 4:
            if ticket is None:
                return httpx.Response(404, json={"message": "not found"})
            shown = {k: v for k, v in ticket.items() if k != "tasks"}
            if request.url.params.get("include") == "requester":  # only when asked, as Freshservice does
                shown["requester"] = {"id": 4711, "name": "Pat Example", "email": "pat.example@example.com"}
            return httpx.Response(200, json={"ticket": shown})
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
        if request.method == "PUT" and len(parts) == 4:
            if ticket is None:
                return httpx.Response(404, json={"message": "not found"})
            if ticket.get("rejects_update"):
                return httpx.Response(400, json={"description": "Validation failed", "errors": [
                    {"field": "sub_category", "message": "Invalid value", "code": "invalid_value"}]})
            for field, value in body.items():
                if field == "status" and ticket.get("ignores_status"):
                    continue
                if field == "resolution_notes" and ticket.get("ignores_resolution"):
                    continue
                if field == "custom_fields":
                    if not ticket.get("ignores_location"):
                        ticket["custom_fields"] = {**(ticket.get("custom_fields") or {}), **value}
                    continue
                ticket[field] = value
            return httpx.Response(200, json={"ticket": {k: v for k, v in ticket.items() if k != "tasks"}})
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

EXPECTED_TOOLS = server.READONLY_TOOLS | {
    "update_ticket_task_status", "complete_term_tasks", "create_ticket_note", "update_ticket_status_category"}
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
                assert r.json() == {"status": "ok", "mode": "per-user", "tools": 28, "output": "full"}, r.json()

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

            # --- update_ticket_status_category ---
            async def fix(**arguments):
                is_error, text = await call_tool(client, "tok-a", "update_ticket_status_category", arguments)
                return is_error, (text if is_error else json.loads(text))

            async def fix_status_sends_only_the_status_code():
                steps = [("Work In Progress", 9), ("On Hold", 8), ("Pending Vendor", 7), ("Pending Approval", 10),
                         ("Pending Customer Response", 6), ("Resolved", 4), ("Open", 2)]
                for (name, code), before in zip(steps, ["Open"] + [n for n, _ in steps]):
                    mark = len(outbound)
                    is_error, r = await fix(ticket_id=60009, status=name)
                    assert not is_error, r
                    puts = since(mark, FS, "PUT")
                    assert [(p["path"], p["body"], p["auth"]) for p in puts] == [
                        ("/api/v2/tickets/60009", {"status": code}, basic(KEY_A))], puts
                    assert r["changes"] == {"status": {"from": before, "to": name}}, r
                    assert r["line"] == f"Updated [Ticket #INC-60009]({URL}/60009): status {before} to {name}.", r

            async def fix_category_path_sends_what_was_given():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60009, category="Software", sub_category="Office", item_category="Excel")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [
                    {"category": "Software", "sub_category": "Office", "item_category": "Excel"}]
                assert r["changes"]["category"] == {"from": "Hardware", "to": "Software"}, r
                assert r["changes"]["item_category"] == {"from": "Dell", "to": "Excel"}, r
                assert (TICKETS[60009]["category"], TICKETS[60009]["sub_category"]) == ("Software", "Office")

            async def fix_changes_a_lower_level_alone():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60010, sub_category="Scanner")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [{"sub_category": "Scanner"}]
                assert TICKETS[60010]["category"] == "Hardware", "category was touched"

            async def fix_sets_category_on_a_ticket_with_none():
                is_error, r = await fix(ticket_id=60013, category="Software")
                assert not is_error and r["changes"] == {"category": {"from": "(none)", "to": "Software"}}, r

            async def fix_changes_status_and_category_in_one_call():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60010, status="On Hold", category="Software", sub_category="Office")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [
                    {"category": "Software", "sub_category": "Office", "status": 8}]

            async def fix_refuses_a_category_without_its_sub_levels():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60012, category="Software")
                assert "sub_category" in r["error"] and "Nothing was changed" in r["error"], r
                is_error, r = await fix(ticket_id=60012, category="Software", sub_category="Office")  # no item on this ticket: fine
                assert not is_error and "error" not in r, r
                before = len(since(mark, FS, "PUT"))
                is_error, r = await fix(ticket_id=60009, sub_category="Printer")  # the ticket has an item_category
                assert "item_category" in r["error"], r
                assert len(since(mark, FS, "PUT")) == before, "a PUT went out for a refused change"

            async def fix_does_nothing_when_nothing_differs():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60013, category="software", status="Open")
                assert r["changed"] is False and "Nothing was changed" in r["line"], r
                assert not since(mark, FS, "PUT")

            async def fix_refuses_bad_input_without_a_call():
                bad = [dict(ticket_id=60010), dict(ticket_id=60010, category="  "), dict(ticket_id=60010, sub_category=""),
                       dict(ticket_id=60010, status="in progress"), dict(ticket_id=60010, status="done"),
                       dict(ticket_id=60010, status=3), dict(ticket_id=60010, status=""),
                       dict(ticket_id=60010, status="Closed"), dict(ticket_id=60010, status="Pending")]  # not in the agent dropdown
                for arguments in bad:
                    mark = len(outbound)
                    is_error, r = await fix(**arguments)
                    assert is_error or "error" in r, (arguments, r)
                    assert not since(mark, FS), f"Freshservice was called for {arguments}"

            async def fix_refuses_a_missing_ticket():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=99999, status="On Hold")
                assert "Couldn't read ticket 99999" in r["error"], r
                assert not since(mark, FS, "PUT")

            async def fix_passes_freshservice_errors_on_and_changes_nothing():
                is_error, r = await fix(ticket_id=60011, sub_category="Scanner")
                assert "Failed to update" in r["error"] and r["details"]["errors"][0]["field"] == "sub_category", r
                assert TICKETS[60011]["sub_category"] == "Printer"

            async def fix_reports_what_freshservice_actually_saved():
                is_error, r = await fix(ticket_id=60012, status="Resolved")
                assert "did not save status as asked" in r["line"], r

            async def fix_has_no_way_to_write_other_fields():
                mark = len(outbound)
                await fix(ticket_id=60010, status="Open", priority=4, responder_id=1)
                put_bodies = [p["body"] for p in since(mark, FS, "PUT")]
                assert all(set(b) <= {"status", "category", "sub_category", "item_category", "resolution_notes", "responder_id", "custom_fields"} for b in put_bodies), put_bodies
                assert not any("custom_fields" in b for b in put_bodies), "a custom field was written without a location"
                assert not any("responder_id" in b for b in put_bodies), "an agent was assigned without assign_to_me"

            async def fix_assigns_to_the_key_owner_and_nobody_else():
                for token, key, ticket_id in [("tok-a", KEY_A, 60014), ("tok-b", KEY_B, 60015)]:
                    mark = len(outbound)
                    is_error, text = await call_tool(client, token, "update_ticket_status_category",
                                                     {"ticket_id": ticket_id, "assign_to_me": True, "responder_id": 1})
                    r = json.loads(text)
                    me = AGENT_IDS[key]
                    puts = since(mark, FS, "PUT")
                    assert [(p["path"], p["body"], p["auth"]) for p in puts] == [
                        (f"/api/v2/tickets/{ticket_id}", {"responder_id": me}, basic(key))], puts
                    assert [o["path"] for o in since(mark, FS, "GET")][0] == f"/api/v2/tickets/{ticket_id}"
                    assert "/api/v2/agents/me" in [o["path"] for o in since(mark, FS, "GET")]
                    assert r["changes"]["assigned_to"]["to"] == "you", r
                assert TICKETS[60014]["responder_id"] == 5001 and TICKETS[60015]["responder_id"] == 5002

            async def fix_reports_who_it_took_the_ticket_from():
                is_error, r = await fix(ticket_id=60017, assign_to_me=True)  # unassigned
                assert r["changes"] == {"assigned_to": {"from": "(unassigned)", "to": "you"}}, r
                assert r["line"].endswith("assigned_to (unassigned) to you."), r
                TICKETS[60017]["responder_id"] = 888
                is_error, r = await fix(ticket_id=60017, assign_to_me=True)  # someone else's
                assert r["changes"] == {"assigned_to": {"from": "agent 888", "to": "you"}}, r

            async def fix_assignment_can_ride_with_status_and_category():
                mark = len(outbound)
                TICKETS[60014]["responder_id"] = None
                is_error, r = await fix(ticket_id=60014, status="Resolved", category="Software", sub_category="Office", assign_to_me=True)
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [
                    {"category": "Software", "sub_category": "Office", "status": 4, "responder_id": 5001}]

            async def fix_assign_does_nothing_when_already_mine():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60016, assign_to_me=True)
                assert r["changed"] is False, r
                assert not since(mark, FS, "PUT")

            async def fix_changes_nothing_when_it_cannot_tell_who_you_are():
                mark = len(outbound)
                agent_lookup_fails["on"] = True
                try:
                    is_error, r = await fix(ticket_id=60013, status="On Hold", assign_to_me=True)
                finally:
                    agent_lookup_fails["on"] = False
                assert "Couldn't tell which Freshservice agent you are" in r["error"] and "Nothing was changed" in r["error"], r
                assert not since(mark, FS, "PUT"), "the status was changed though the assignment could not be"

            async def fix_resolves_with_a_resolution_note():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60018, status="Resolved", resolution_note="  Replaced the toner and ran a test page.\nAll good. ")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [
                    {"status": 4, "resolution_notes": "Replaced the toner and ran a test page.\nAll good."}]
                assert r["changes"]["resolution_note"] == {"from": "(none)", "to": "Replaced the toner and ran a test page. All good."}, r
                assert TICKETS[60018]["resolution_notes"].startswith("Replaced the toner")

            async def fix_resolution_note_alone_and_unchanged_are_handled():
                ticket = TICKETS[60018]
                ticket["resolution_notes"] = None
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60018, resolution_note="Reset the spooler")
                assert [p["body"] for p in since(mark, FS, "PUT")] == [{"resolution_notes": "Reset the spooler"}]
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60018, resolution_note="Reset the spooler ")  # same text
                assert r["changed"] is False and not since(mark, FS, "PUT"), r
                is_error, r = await fix(ticket_id=60018, resolution_note="reset the spooler")  # only the capital differs
                assert r["changed"] is True and len(since(mark, FS, "PUT")) == 1, r

            async def fix_long_resolution_note_is_shortened_in_the_report_only():
                text = "word " * 60
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60014, resolution_note=text)
                assert since(mark, FS, "PUT")[0]["body"]["resolution_notes"] == text.strip()
                assert r["changes"]["resolution_note"]["to"].endswith("...") and len(r["changes"]["resolution_note"]["to"]) == 60, r

            async def fix_refuses_a_blank_resolution_note_without_a_call():
                for blank in ["", "   ", "\n"]:
                    mark = len(outbound)
                    is_error, r = await fix(ticket_id=60018, resolution_note=blank)
                    assert is_error or "error" in r, (blank, r)
                    assert not since(mark, FS), "Freshservice was called for a blank resolution note"

            async def fix_reports_a_resolution_note_freshservice_did_not_save():
                is_error, r = await fix(ticket_id=60019, resolution_note="Fixed it")
                assert "did not save resolution_note as asked" in r["line"], r

            def fs_paths(mark):
                return [(o["method"], o["path"]) for o in since(mark, FS)]

            async def fix_sets_location_by_name_across_pages():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60020, location="  portland ")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [{"custom_fields": {"lf_location": 33000357140}}]
                assert fs_paths(mark).count(("GET", "/api/v2/locations")) == 2, fs_paths(mark)  # 135 locations: two pages
                assert r["changes"] == {"location": {"from": "(none)", "to": "Portland"}}, r
                assert r["line"].endswith("location (none) to Portland."), r
                assert TICKETS[60020]["custom_fields"]["lf_location"] == 33000357140

            async def fix_sets_location_by_id_without_a_lookup():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60023, location="4242")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [{"custom_fields": {"lf_location": 4242}}]
                assert ("GET", "/api/v2/locations") not in fs_paths(mark), "an id needed no lookup"
                assert r["changes"]["location"] == {"from": "(none)", "to": "location 4242"}, r

            async def fix_location_name_must_match_one_location_exactly():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60020, location="Seattle")  # exact beats "Seattle Annex"
                assert r["changes"]["location"]["to"] == "Seattle", r
                before = len(since(mark, FS, "PUT"))
                is_error, r = await fix(ticket_id=60021, location="Seat")
                assert "No location named 'Seat'" in r["error"] and "Seattle, Seattle Annex" in r["error"], r
                is_error, r = await fix(ticket_id=60021, location="Nowhere")
                assert "No location named 'Nowhere'" in r["error"] and "Close matches" not in r["error"], r
                is_error, r = await fix(ticket_id=60021, location="TWIN")  # two locations differ only by case
                assert "More than one location" in r["error"] and "5001" in r["error"] and "5002" in r["error"], r
                assert len(since(mark, FS, "PUT")) == before, "a PUT went out for a location that didn't resolve"

            async def fix_location_lookup_failure_changes_nothing_and_ids_still_work():
                mark = len(outbound)
                locations_forbidden["on"] = True
                try:
                    is_error, r = await fix(ticket_id=60021, status="On Hold", location="Seattle")
                    assert "Couldn't look up locations" in r["error"] and "numeric location id" in r["error"], r
                    assert not since(mark, FS, "PUT"), "the status changed though the location could not be looked up"
                    is_error, r = await fix(ticket_id=60022, location="4243")  # ids need no lookup
                    assert not is_error and "error" not in r, r
                finally:
                    locations_forbidden["on"] = False

            async def fix_location_unchanged_sends_nothing():
                mark = len(outbound)
                is_error, r = await fix(ticket_id=60021, location="Portland")
                assert r["changed"] is False and not since(mark, FS, "PUT"), r

            async def fix_location_rides_with_assignment_status_and_resolution():
                mark = len(outbound)
                TICKETS[60023]["responder_id"] = None
                is_error, r = await fix(ticket_id=60023, location="Portland", assign_to_me=True, status="Resolved", resolution_note="Done")
                assert not is_error, r
                assert [p["body"] for p in since(mark, FS, "PUT")] == [
                    {"status": 4, "resolution_notes": "Done", "responder_id": 5001, "custom_fields": {"lf_location": 33000357140}}]
                assert r["changes"]["location"] == {"from": "location 4242", "to": "Portland"}, r

            async def fix_refuses_a_blank_location_without_a_call():
                for blank in ["", "  "]:
                    mark = len(outbound)
                    is_error, r = await fix(ticket_id=60020, location=blank)
                    assert is_error or "error" in r, (blank, r)
                    assert not since(mark, FS), "Freshservice was called for a blank location"

            async def fix_reports_a_location_freshservice_did_not_save():
                is_error, r = await fix(ticket_id=60022, location="Seattle")
                assert "did not save location as asked" in r["line"], r

            async def ticket_has_no_requester_details_unless_asked():
                r = await tool("get_ticket_by_id", ticket_id=60008)
                assert "requester" not in r["ticket"], r["ticket"].keys()

            async def ticket_can_carry_the_requesters_name_and_email():
                r = await tool("get_ticket_by_id", ticket_id=60008, include_requester=True)
                assert r["ticket"]["requester"] == {"id": 4711, "name": "Pat Example", "email": "pat.example@example.com"}, r["ticket"].get("requester")
                with compact():
                    c = await tool("get_ticket_by_id", ticket_id=60008, include_requester=True)
                assert "description" not in c["ticket"] and c["ticket"]["requester"]["email"] == "pat.example@example.com", c["ticket"].keys()

            async def fix_without_assign_to_me_never_looks_up_the_agent():
                mark = len(outbound)
                await fix(ticket_id=60013, status="Work In Progress")
                assert "/api/v2/agents/me" not in [o["path"] for o in since(mark, FS)]

            async def term_rejects_a_non_numeric_employee_id():
                mark = len(outbound)
                is_error, r = await term(ticket_id=60005, employee_id="abc", systems_without_account=["SAP"])
                assert "must be a number" in r["error"], r
                assert not since(mark, FS), "Freshservice was called for a bad employee ID"

            # --- compact output -------------------------------------------------------

            async def tool(name, **arguments):
                is_error, text = await call_tool(client, "tok-a", name, arguments)
                assert not is_error, text
                return json.loads(text)

            class compact:
                """Switch the server to compact output for one test, then back."""
                def __enter__(self):
                    server.OUTPUT_MODE = server.OUTPUT_COMPACT
                def __exit__(self, *exc):
                    server.OUTPUT_MODE = server.OUTPUT_FULL

            expected_keys = list(server.COMPACT_TICKET_FIELDS) + ["description_preview"]

            async def full_output_is_unchanged_by_default():
                assert server.OUTPUT_MODE == "full", server.OUTPUT_MODE
                assert await tool("filter_tickets", query="status:2") == {"tickets": LIST_PAGE, "total": 30}
                paged = await tool("get_tickets", page=1, per_page=30)
                assert paged["tickets"] == LIST_PAGE and paged["pagination"]["next_page"] == 2, paged["pagination"]
                single = await tool("get_ticket_by_id", ticket_id=60008)
                assert single == {"ticket": {k: v for k, v in TICKETS[60008].items() if k != "tasks"}}, single
                assert await tool("list_all_ticket_conversation", ticket_id=60008) == CONVERSATIONS

            async def compact_list_records_have_only_the_listed_fields():
                with compact():
                    r = await tool("filter_tickets", query="status:2")
                assert r["total"] == 30 and len(r["tickets"]) == 30, (r.get("total"), len(r.get("tickets", [])))
                for record, source in zip(r["tickets"], LIST_PAGE):
                    assert list(record.keys()) == expected_keys, list(record.keys())
                    for field in server.COMPACT_TICKET_FIELDS:
                        assert record[field] == source[field], field
                    preview = record["description_preview"]
                    assert len(preview) <= 500 and "\n" not in preview and "  " not in preview, repr(preview[:60])
                    assert preview.startswith(f"Line one of ticket {source['id'] - 70000}. Line two, with spaces."), repr(preview[:60])
                    assert "description" not in record and "custom_fields" not in record

            async def compact_page_of_30_is_small():
                with compact():
                    small = await tool("filter_tickets", query="status:2")
                big = await tool("filter_tickets", query="status:2")
                assert len(json.dumps(small)) < 40_000, len(json.dumps(small))
                assert len(json.dumps(big)) > 500_000, len(json.dumps(big))

            async def compact_get_tickets_keeps_pagination():
                with compact():
                    r = await tool("get_tickets", page=1, per_page=30)
                assert r["pagination"] == {"current_page": 1, "next_page": 2, "prev_page": None, "per_page": 30}, r["pagination"]
                assert all(list(t.keys()) == expected_keys for t in r["tickets"]), [list(t.keys()) for t in r["tickets"][:1]]

            async def fields_all_returns_full_records_in_compact_mode():
                with compact():
                    assert await tool("filter_tickets", query="status:2", fields="all") == {"tickets": LIST_PAGE, "total": 30}
                    paged = await tool("get_tickets", page=1, per_page=30, fields="all")
                    assert paged["tickets"] == LIST_PAGE

            async def fields_other_than_all_is_refused_without_a_call():
                for name, args in [("filter_tickets", {"query": "status:2"}), ("get_tickets", {"page": 1})]:
                    mark = len(outbound)
                    r = await tool(name, fields="summary", **args)
                    assert r == {"error": "fields must be 'all' or left out."}, r
                    assert not since(mark, FS), "Freshservice was called despite a bad fields value"

            async def compact_single_ticket_drops_only_the_html():
                with compact():
                    r = await tool("get_ticket_by_id", ticket_id=60008)
                full = {k: v for k, v in TICKETS[60008].items() if k != "tasks"}
                assert r == {"ticket": {k: v for k, v in full.items() if k != "description"}}, r
                assert r["ticket"]["description_text"] == "Hello fan noise" and r["ticket"]["custom_fields"] == {"lf_location": 1}

            async def compact_conversations_drop_only_the_html_body():
                with compact():
                    r = await tool("list_all_ticket_conversation", ticket_id=60008)
                expected = {**CONVERSATIONS, "conversations": [{k: v for k, v in c.items() if k != "body"} for c in CONVERSATIONS["conversations"]]}
                assert r == expected, r
                assert [c["body_text"] for c in r["conversations"]] == ["Hi there", "Fixed"]

            async def compact_mode_leaves_the_term_tool_alone():
                # A preview changes nothing, so the same call must answer the same in both modes.
                _, in_full = await term(ticket_id=60001, employee_id="11116", systems_without_account=["Titan"], preview=True)
                with compact():
                    is_error, in_compact = await term(ticket_id=60001, employee_id="11116", systems_without_account=["Titan"], preview=True)
                assert not is_error and in_compact == in_full and in_compact["preview"] is True, (in_full, in_compact)
                assert any("No Titan account" in line for line in in_compact["lines"]), in_compact["lines"]

            # --- search_tickets -------------------------------------------------------

            async def search(**arguments):
                arguments.setdefault("created_after", "2026-06-01")
                arguments.setdefault("created_before", "2026-06-19")
                return await tool("search_tickets", **arguments)

            def ids(r):
                return [t["id"] for t in r["matches"]]

            async def search_finds_words_in_subject_or_body():
                r = await search(words="Jane Doe")
                assert ids(r) == [80002, 80040], ids(r)
                assert (r["matched"], r["scanned"], r["pages"], r["total_in_window"], r["truncated"]) == (2, 70, 3, 70, False), r
                assert r["oldest_scanned"] == SEARCH_TICKETS[69]["created_at"], r["oldest_scanned"]
                assert r["query"] == "created_at:>'2026-06-01' AND created_at:<'2026-06-19'", r["query"]
                assert all(list(t.keys()) == expected_keys for t in r["matches"]), [list(t.keys()) for t in r["matches"]]
                assert "<div" not in json.dumps(r) and not any("description" in t for t in r["matches"])

            async def search_matches_body_phrases_and_case():
                assert ids(await search(words="TITAN invoices")) == [80035]
                assert ids(await search(words='"post invoices"')) == [80035]
                assert ids(await search(words='"invoices post"')) == []
                assert ids(await search(words="titan")) == [80035, 80040]
                assert ids(await search(words="printer 7")) == [80007, 80017, 80027, 80037, 80047, 80057, 80067]

            async def search_passes_narrowing_to_freshservice():
                mark = len(search_queries)
                r = await search(words="", requester_email="notify@hr.example.com", source=1, group_id=33, status=5)
                expected = ("created_at:>'2026-06-01' AND created_at:<'2026-06-19' AND email:'notify@hr.example.com'"
                            " AND source:1 AND group_id:33 AND status:5")
                assert search_queries[mark:] == [expected] * 3, search_queries[mark:]
                assert r["query"] == expected and r["scanned"] == 70, r["query"]

            async def search_filters_category_on_the_server():
                assert ids(await search(words="", category="human resources", sub_category="SEPARATION")) == [80065]
                assert ids(await search(words="", category="Human Resources")) == [80002, 80065]
                assert ids(await search(words="Jane", category="Human Resources")) == [80002]

            async def search_page_cap_is_reported():
                r = await search(words="1234", max_pages=2)
                assert ids(r) == [] and r["truncated"] is True and (r["scanned"], r["pages"]) == (60, 2), r
                assert r["oldest_scanned"] == SEARCH_TICKETS[59]["created_at"], r["oldest_scanned"]
                r = await search(words="1234", max_pages=3)
                assert ids(r) == [80065] and r["truncated"] is False, r

            async def search_match_cap_is_reported():
                cap = server.SEARCH_MATCH_CAP
                server.SEARCH_MATCH_CAP = 5
                try:
                    r = await search(words="printer")
                finally:
                    server.SEARCH_MATCH_CAP = cap
                # Five matches by the sixth ticket (the third is the Doe request): the scan stops there,
                # so "scanned" and "oldest_scanned" say where to continue from, not what page was fetched.
                assert (r["matched"], r["truncated"], r["pages"], r["scanned"]) == (5, True, 1, 6), r
                assert r["oldest_scanned"] == SEARCH_TICKETS[5]["created_at"], r["oldest_scanned"]

            async def search_refuses_bad_input_without_a_call():
                bad = [
                    dict(words="x", created_after="2026-13-01"),
                    dict(words="x", created_after="20260601"),
                    dict(words="x", created_after="2026-06-01", created_before="2026-05-31"),
                    dict(words="x", created_after="2026-06-01", max_pages=0),
                    dict(words="x", created_after="2026-06-01", max_pages=41),
                    dict(words="x", created_after="2026-06-01", fields="summary"),
                    dict(words="", created_after="2026-06-01"),
                    dict(words='""', created_after="2026-06-01"),
                    dict(words="x", created_after="2026-06-01", requester_email="not an email"),
                ]
                for arguments in bad:
                    mark = len(outbound)
                    r = await tool("search_tickets", **arguments)
                    assert "error" in r, (arguments, r)
                    assert not since(mark, FS), f"Freshservice was called for {arguments}"

            async def search_fields_all_returns_full_records():
                r = await search(words="Jane Doe", fields="all")
                assert r["matches"] == [SEARCH_TICKETS[2], SEARCH_TICKETS[40]], ids(r)

            async def search_is_the_same_in_compact_mode():
                in_full = await search(words="titan")
                with compact():
                    in_compact = await search(words="titan")
                assert in_full == in_compact

            async def search_defaults_created_before_to_tomorrow():
                r = await tool("search_tickets", words="Jane", created_after="2026-06-01")
                tomorrow = (server.date.today() + server.timedelta(days=1)).isoformat()
                assert r["query"] == f"created_at:>'2026-06-01' AND created_at:<'{tomorrow}'", r["query"]
                assert ids(r) == [80002, 80040]

            async def search_reports_a_freshservice_error_with_what_it_had():
                r = await tool("search_tickets", words="printer", created_after="2026-02-01", created_before="2026-02-28")
                assert "error" in r and r["details"] == {"description": "boom"} and r["pages_read"] == 1, r
                assert [t["id"] for t in r["matches_so_far"]] == [80000 + i for i in range(30) if i not in (2,)], r["matches_so_far"][:3]

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
                fix_status_sends_only_the_status_code,
                fix_category_path_sends_what_was_given,
                fix_changes_a_lower_level_alone,
                fix_sets_category_on_a_ticket_with_none,
                fix_changes_status_and_category_in_one_call,
                fix_refuses_a_category_without_its_sub_levels,
                fix_does_nothing_when_nothing_differs,
                fix_refuses_bad_input_without_a_call,
                fix_refuses_a_missing_ticket,
                fix_passes_freshservice_errors_on_and_changes_nothing,
                fix_reports_what_freshservice_actually_saved,
                fix_has_no_way_to_write_other_fields,
                fix_assigns_to_the_key_owner_and_nobody_else,
                fix_reports_who_it_took_the_ticket_from,
                fix_assignment_can_ride_with_status_and_category,
                fix_assign_does_nothing_when_already_mine,
                fix_changes_nothing_when_it_cannot_tell_who_you_are,
                fix_without_assign_to_me_never_looks_up_the_agent,
                fix_resolves_with_a_resolution_note,
                fix_resolution_note_alone_and_unchanged_are_handled,
                fix_long_resolution_note_is_shortened_in_the_report_only,
                fix_refuses_a_blank_resolution_note_without_a_call,
                fix_reports_a_resolution_note_freshservice_did_not_save,
                ticket_has_no_requester_details_unless_asked,
                ticket_can_carry_the_requesters_name_and_email,
                fix_sets_location_by_name_across_pages,
                fix_sets_location_by_id_without_a_lookup,
                fix_location_name_must_match_one_location_exactly,
                fix_location_lookup_failure_changes_nothing_and_ids_still_work,
                fix_location_unchanged_sends_nothing,
                fix_location_rides_with_assignment_status_and_resolution,
                fix_refuses_a_blank_location_without_a_call,
                fix_reports_a_location_freshservice_did_not_save,
                full_output_is_unchanged_by_default,
                compact_list_records_have_only_the_listed_fields,
                compact_page_of_30_is_small,
                compact_get_tickets_keeps_pagination,
                fields_all_returns_full_records_in_compact_mode,
                fields_other_than_all_is_refused_without_a_call,
                compact_single_ticket_drops_only_the_html,
                compact_conversations_drop_only_the_html_body,
                compact_mode_leaves_the_term_tool_alone,
                search_finds_words_in_subject_or_body,
                search_matches_body_phrases_and_case,
                search_passes_narrowing_to_freshservice,
                search_filters_category_on_the_server,
                search_page_cap_is_reported,
                search_match_cap_is_reported,
                search_refuses_bad_input_without_a_call,
                search_fields_all_returns_full_records,
                search_is_the_same_in_compact_mode,
                search_defaults_created_before_to_tomorrow,
                search_reports_a_freshservice_error_with_what_it_had,
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
    """The Unraid deployment: no FRESHSERVICE_KEY_MODE, a shared key, the 24 read tools."""
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
    assert len(registered) == 24, len(registered)
    assert "search_tickets" in registered and "get_requested_items" in registered
    assert "update_ticket_task_status" not in registered
    assert "complete_term_tasks" not in registered
    assert "create_ticket_note" not in registered
    assert "update_ticket_status_category" not in registered


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


def test_unknown_output_mode_refuses_to_start():
    r = _python("from freshservice_mcp.server import main; main()", {"FRESHSERVICE_OUTPUT": "small", "OWUI_URL": OWUI_URL})
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "FRESHSERVICE_OUTPUT='small' is not one of full, compact" in r.stderr, r.stderr[-500:]


def test_output_mode_is_full_unless_asked_for_in_either_key_mode():
    code = "from freshservice_mcp.server import OUTPUT_MODE, KEY_MODE; print(KEY_MODE, OUTPUT_MODE)"
    shared = _python(code, {"FRESHSERVICE_APIKEY": "S" * 20}, drop=("FRESHSERVICE_KEY_MODE", "FRESHSERVICE_OUTPUT"))
    assert shared.stdout.strip().splitlines()[-1] == "shared full", shared.stdout
    shared_compact = _python(code, {"FRESHSERVICE_APIKEY": "S" * 20, "FRESHSERVICE_OUTPUT": "compact"}, drop=("FRESHSERVICE_KEY_MODE",))
    assert shared_compact.stdout.strip().splitlines()[-1] == "shared compact", shared_compact.stdout
    per_user = _python(code, {"FRESHSERVICE_OUTPUT": " Compact "})
    assert per_user.stdout.strip().splitlines()[-1] == "per-user compact", per_user.stdout


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
