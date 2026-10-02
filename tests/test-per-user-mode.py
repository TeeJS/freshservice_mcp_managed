"""Per-user key mode tests. No Freshservice connection needed.

Freshservice is replaced by an in-memory fake, so every outbound request can be
inspected: which key it carried, what body it sent, whether it was sent at all.

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

KEY_A = "A" * 20
KEY_B = "B" * 20
KEY_WRONG = "W" * 20
KEY_ERROR = "E" * 20
GOOD_KEYS = {KEY_A, KEY_B}


def basic(key):
    return "Basic " + base64.b64encode(f"{key}:X".encode()).decode()


# --- Fake Freshservice -------------------------------------------------------

outbound = []  # every request that would have reached Freshservice


async def fake_freshservice(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(0.005)  # let concurrent requests interleave
    auth = request.headers.get("authorization", "")
    body = json.loads(request.content) if request.content else None
    outbound.append({"method": request.method, "path": request.url.path, "auth": auth, "body": body})

    if auth == basic(KEY_ERROR):
        return httpx.Response(500, json={"message": "server error"})
    if auth not in {basic(k) for k in GOOD_KEYS}:
        return httpx.Response(401, json={"message": "invalid credentials"})

    path = request.url.path
    if request.method == "GET" and path == "/api/v2/tickets":
        return httpx.Response(200, json={"tickets": []})
    if request.method == "GET" and path.endswith("/tasks"):
        return httpx.Response(200, json={"tasks": [], "seen_auth": auth})
    if request.method == "PUT" and "/tasks/" in path:
        return httpx.Response(200, json={"task": {"id": int(path.rsplit("/", 1)[1]), "status": body["status"]}})
    return httpx.Response(404, json={"message": "not found"})


_RealAsyncClient = httpx.AsyncClient


class FakeAsyncClient(_RealAsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(fake_freshservice))
        super().__init__(*args, **kwargs)


httpx.AsyncClient = FakeAsyncClient

# Capture every log line so we can prove no key is ever written.
log_lines = []


class _Capture(logging.Handler):
    def emit(self, record):
        log_lines.append(self.format(record))


logging.getLogger().addHandler(_Capture())
logging.getLogger().setLevel(logging.DEBUG)

from starlette.testclient import TestClient  # noqa: E402

from freshservice_mcp import per_user, server  # noqa: E402

EXPECTED_TOOLS = server.READONLY_TOOLS | {"update_ticket_task_status"}


def new_client():
    return TestClient(per_user.build_app(server.mcp, "example.freshservice.com"))


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


# --- Tests -------------------------------------------------------------------

def test_healthz_needs_no_key():
    r = new_client().get("/healthz")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "mode": "per-user", "tools": len(EXPECTED_TOOLS)}, r.json()


def test_no_key_is_refused_without_calling_freshservice():
    client = new_client()
    before = len(outbound)
    assert client.get("/openapi.json").status_code == 401
    assert client.post("/get_ticket_tasks", json={"ticket_id": 1}).status_code == 401
    assert client.post("/get_ticket_tasks", json={"ticket_id": 1}, headers={"Authorization": "Basic abc"}).status_code == 401
    assert len(outbound) == before, "a request without a key reached Freshservice"


def test_malformed_keys_never_reach_freshservice():
    client = new_client()
    before = len(outbound)
    for bad in ["short", "has a space in it 123", "abc$%^&*()1234567890", "x" * 65]:
        r = client.post("/get_ticket_tasks", json={"ticket_id": 1}, headers=bearer(bad))
        assert r.status_code == 401, (bad, r.status_code)
    assert len(outbound) == before, "a malformed key reached Freshservice"


def test_openapi_lists_exactly_the_per_user_tools():
    r = new_client().get("/openapi.json", headers=bearer(KEY_A))
    assert r.status_code == 200, r.text
    ops = {op["post"]["operationId"] for op in r.json()["paths"].values()}
    assert ops == EXPECTED_TOOLS, ops ^ EXPECTED_TOOLS
    assert len(ops) == 23, len(ops)
    for disabled in ["create_ticket", "update_ticket", "delete_ticket", "update_change_task"]:
        assert disabled not in ops, disabled


def test_disabled_write_tools_are_refused_by_name():
    client = new_client()
    before = len(outbound)
    for name in ["create_ticket", "update_ticket", "delete_ticket", "update_change_task"]:
        r = client.post(f"/{name}", json={}, headers=bearer(KEY_A))
        assert r.status_code == 404, (name, r.status_code)
    assert len(outbound) == before, "a disabled tool reached Freshservice"


def test_each_call_uses_the_callers_own_key():
    client = new_client()
    for key in [KEY_A, KEY_B, KEY_A]:
        before = len(outbound)
        r = client.post("/get_ticket_tasks", json={"ticket_id": 47071}, headers=bearer(key))
        assert r.status_code == 200, r.text
        assert r.json()["seen_auth"] == basic(key)
        assert all(o["auth"] == basic(key) for o in outbound[before:])


def test_concurrent_callers_never_see_each_others_key():
    app = per_user.build_app(server.mcp, "example.freshservice.com")

    async def run():
        async with _RealAsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            keys = [KEY_A, KEY_B] * 10
            responses = await asyncio.gather(*[
                client.post("/get_ticket_tasks", json={"ticket_id": 47071}, headers=bearer(k)) for k in keys
            ])
            return list(zip(keys, responses))

    for key, r in asyncio.run(run()):
        assert r.status_code == 200, r.text
        assert r.json()["seen_auth"] == basic(key), "a call ran with another caller's key"


def test_wrong_key_is_checked_once_then_refused_locally():
    client = new_client()
    before = len(outbound)
    r1 = client.post("/get_ticket_tasks", json={"ticket_id": 1}, headers=bearer(KEY_WRONG))
    after_first = len(outbound)
    r2 = client.post("/get_ticket_tasks", json={"ticket_id": 1}, headers=bearer(KEY_WRONG))
    assert r1.status_code == 401 and r2.status_code == 401, (r1.status_code, r2.status_code)
    assert after_first - before == 1, "wrong key should get exactly one Freshservice check"
    assert len(outbound) == after_first, "a known-wrong key reached Freshservice again"


def test_freshservice_error_is_not_cached():
    client = new_client()
    before = len(outbound)
    assert client.get("/openapi.json", headers=bearer(KEY_ERROR)).status_code == 503
    assert client.get("/openapi.json", headers=bearer(KEY_ERROR)).status_code == 503
    assert len(outbound) - before == 2, "an inconclusive check should be retried, not cached"


def test_new_key_checks_are_throttled():
    saved = per_user.KEY_CHECKS_PER_MINUTE
    per_user.KEY_CHECKS_PER_MINUTE = 2
    try:
        client = new_client()
        before = len(outbound)
        codes = [
            client.get("/openapi.json", headers=bearer(c * 20)).status_code
            for c in ["C", "D", "F"]  # three keys nobody has checked yet
        ]
        assert codes[2] == 429, codes
        assert len(outbound) - before == 2, "the throttled check reached Freshservice"
    finally:
        per_user.KEY_CHECKS_PER_MINUTE = saved


def test_task_status_sends_only_the_status():
    client = new_client()
    for status, code in [("completed", 3), ("open", 1)]:
        before = len(outbound)
        r = client.post(
            "/update_ticket_task_status",
            json={"ticket_id": 47071, "task_id": 5587, "status": status},
            headers=bearer(KEY_A),
        )
        assert r.status_code == 200, r.text
        puts = [o for o in outbound[before:] if o["method"] == "PUT"]
        assert len(puts) == 1, puts
        assert puts[0]["path"] == "/api/v2/tickets/47071/tasks/5587", puts[0]["path"]
        assert puts[0]["body"] == {"status": code}, puts[0]["body"]
        assert puts[0]["auth"] == basic(KEY_A)


def test_task_status_refuses_anything_but_open_or_completed():
    client = new_client()
    before = len(outbound)
    for status in ["in progress", "closed", 2, 3, ""]:
        r = client.post(
            "/update_ticket_task_status",
            json={"ticket_id": 47071, "task_id": 5587, "status": status},
            headers=bearer(KEY_A),
        )
        assert r.status_code == 422, (status, r.status_code)
    assert not [o for o in outbound[before:] if o["method"] == "PUT"], "a refused status reached Freshservice"


def test_bad_arguments_are_rejected():
    client = new_client()
    assert client.post("/get_ticket_tasks", content=b"not json", headers=bearer(KEY_A)).status_code == 422
    assert client.post("/get_ticket_tasks", json=[1, 2], headers=bearer(KEY_A)).status_code == 422
    assert client.post("/get_ticket_tasks", json={}, headers=bearer(KEY_A)).status_code == 422


def test_no_fallback_to_a_shared_key():
    saved = server.FRESHSERVICE_APIKEY
    server.FRESHSERVICE_APIKEY = "S" * 20  # as if one had been set anyway
    try:
        try:
            server.get_auth_headers()
        except RuntimeError:
            pass
        else:
            raise AssertionError("get_auth_headers() fell back to the shared key")
        before = len(outbound)
        new_client().post("/get_ticket_tasks", json={"ticket_id": 1}, headers=bearer(KEY_A))
        assert all(o["auth"] == basic(KEY_A) for o in outbound[before:])
    finally:
        server.FRESHSERVICE_APIKEY = saved


def test_logs_never_contain_a_key():
    secrets = [KEY_A, KEY_B, KEY_WRONG, KEY_ERROR]
    secrets += [basic(k).split(" ", 1)[1] for k in secrets]
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


def test_shared_mode_still_uses_the_shared_key():
    r = _python(
        "from freshservice_mcp.server import get_auth_headers; print(get_auth_headers()['Authorization'])",
        {"FRESHSERVICE_APIKEY": "S" * 20},
        drop=("FRESHSERVICE_KEY_MODE",),
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip().splitlines()[-1] == basic("S" * 20)


def test_per_user_mode_refuses_to_start_with_a_shared_key():
    r = _python("from freshservice_mcp.server import main; main()", {"FRESHSERVICE_APIKEY": "S" * 20})
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "FRESHSERVICE_APIKEY is set" in r.stderr, r.stderr[-500:]


def test_per_user_mode_refuses_to_start_without_a_domain():
    r = _python("from freshservice_mcp.server import main; main()", {}, drop=("FRESHSERVICE_DOMAIN",))
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "FRESHSERVICE_DOMAIN is required" in r.stderr, r.stderr[-500:]


def test_unknown_mode_refuses_to_start():
    r = _python("from freshservice_mcp.server import main; main()", {"FRESHSERVICE_KEY_MODE": "peruser"})
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert "is not one of" in r.stderr, r.stderr[-500:]


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS: {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL: {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
