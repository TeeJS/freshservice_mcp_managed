"""Per-user key mode: an OpenAPI tool server for Open WebUI user connections.

In this mode the server holds no Freshservice credential. Each request carries
the caller's own Freshservice API key as ``Authorization: Bearer <key>`` (the
key the user typed into Open WebUI's Settings > Integrations), and every
Freshservice call made while serving that request uses that key and no other.

    open:   /healthz
    keyed:  /openapi.json      -> tool list, built from the registered tools
            POST /<tool name>  -> run one tool with the JSON body as arguments

Open WebUI's user-level tool servers are OpenAPI-only and are called from the
user's browser, so this is an OpenAPI surface rather than MCP.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import logging
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

MODE_SHARED = "shared"
MODE_PER_USER = "per-user"
MODES = (MODE_SHARED, MODE_PER_USER)

# Freshservice API keys are short alphanumeric tokens. Anything else is
# refused here, without a Freshservice call: invalid requests count against the
# account-wide rate limit that every other integration shares.
_KEY_PATTERN = re.compile(r"[A-Za-z0-9]{16,64}")

# How long a key's check result is reused before Freshservice is asked again.
KEY_CHECK_TTL_SECONDS = 600
# Upper bound on remembered check results, so a stream of junk keys cannot grow
# memory without limit.
KEY_CHECK_CACHE_MAX = 1000
# Freshservice checks allowed per minute across all callers. A burst of new
# keys beyond this is refused instead of being passed on to Freshservice.
KEY_CHECKS_PER_MINUTE = 30

# The key for the request being served. Set by the request handler, read by
# get_auth_headers(); a contextvar keeps concurrent requests apart.
_request_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "freshservice_request_api_key", default=None
)


def current_api_key() -> str | None:
    """The Freshservice API key sent with the request being served, if any."""
    return _request_api_key.get()


def basic_auth_value(api_key: str) -> str:
    """Freshservice takes the API key as the Basic-auth username, password X."""
    return "Basic " + base64.b64encode(f"{api_key}:X".encode()).decode()


def _key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _bearer_key(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


class KeyChecker:
    """Confirm a key with Freshservice once, then answer from memory.

    Only a SHA-256 hash of each key is kept, never the key itself.
    """

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._results: dict[str, tuple[bool, float]] = {}
        self._recent_checks: list[float] = []

    def _cached(self, digest: str) -> bool | None:
        entry = self._results.get(digest)
        if entry is None:
            return None
        valid, expires = entry
        if expires < time.monotonic():
            del self._results[digest]
            return None
        return valid

    def _remember(self, digest: str, valid: bool) -> None:
        now = time.monotonic()
        if len(self._results) >= KEY_CHECK_CACHE_MAX:
            self._results = {
                d: entry for d, entry in self._results.items() if entry[1] >= now
            }
            if len(self._results) >= KEY_CHECK_CACHE_MAX:
                self._results.pop(next(iter(self._results)))
        self._results[digest] = (valid, now + KEY_CHECK_TTL_SECONDS)

    def _take_check_slot(self) -> bool:
        now = time.monotonic()
        self._recent_checks = [t for t in self._recent_checks if now - t < 60]
        if len(self._recent_checks) >= KEY_CHECKS_PER_MINUTE:
            return False
        self._recent_checks.append(now)
        return True

    async def check(self, api_key: str) -> JSONResponse | None:
        """Return None if the key may be used, or the error response to send."""
        if not _KEY_PATTERN.fullmatch(api_key):
            return _error(
                401,
                "That doesn't look like a Freshservice API key. Copy it from "
                "Freshservice > Profile Settings > Your API Key.",
            )

        digest = _key_hash(api_key)
        cached = self._cached(digest)
        if cached is True:
            return None
        if cached is False:
            return _error(401, "Freshservice rejected this API key.")

        if not self._take_check_slot():
            log.warning("KEY_CHECK_THROTTLED limit=%d/min", KEY_CHECKS_PER_MINUTE)
            return _error(429, "Too many new keys to check right now. Try again in a minute.")

        url = f"https://{self.domain}/api/v2/tickets"
        headers = {"Authorization": basic_auth_value(api_key)}
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params={"per_page": 1})
        except httpx.HTTPError as exc:
            log.warning("KEY_CHECK_FAILED error=%s", type(exc).__name__)
            return _error(503, "Couldn't reach Freshservice to check the key. Try again.")

        if response.status_code == 401:
            self._remember(digest, False)
            log.info("KEY_REJECTED key=sha256:%s", digest[:12])
            return _error(401, "Freshservice rejected this API key.")
        if response.status_code in (200, 403):
            # 403 still proves the key is genuine; what it may read is enforced
            # by Freshservice on each call.
            self._remember(digest, True)
            log.info("KEY_ACCEPTED key=sha256:%s", digest[:12])
            return None

        log.warning("KEY_CHECK_UNEXPECTED status=%s", response.status_code)
        return _error(503, "Couldn't check the key with Freshservice. Try again.")


def build_openapi(tools: list[Any], title: str) -> dict[str, Any]:
    """An OpenAPI document with one POST operation per registered tool."""
    paths: dict[str, Any] = {}
    for tool in tools:
        schema = dict(tool.parameters)
        schema.pop("title", None)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        paths[f"/{tool.name}"] = {
            "post": {
                "operationId": tool.name,
                "summary": tool.name.replace("_", " "),
                "description": tool.description or tool.name,
                "requestBody": {
                    "required": bool(schema.get("required")),
                    "content": {"application/json": {"schema": schema}},
                },
                "responses": {
                    "200": {"description": "Tool result"},
                    "401": {"description": "Missing or rejected Freshservice API key"},
                    "422": {"description": "Invalid arguments"},
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": "1.0.0",
            "description": "Freshservice tools that run with your own Freshservice API key.",
        },
        "paths": paths,
        "components": {
            "securitySchemes": {"freshserviceKey": {"type": "http", "scheme": "bearer"}}
        },
        "security": [{"freshserviceKey": []}],
    }


def build_app(mcp, domain: str) -> Starlette:
    """The per-user ASGI app, serving exactly the tools registered on ``mcp``."""
    checker = KeyChecker(domain)
    tool_manager = mcp._tool_manager

    def tools() -> list[Any]:
        return tool_manager.list_tools()

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": MODE_PER_USER, "tools": len(tools())})

    async def authorize(request: Request) -> tuple[str | None, JSONResponse | None]:
        api_key = _bearer_key(request)
        if api_key is None:
            return None, _error(
                401,
                "Add your Freshservice API key as the Bearer key for this tool server "
                "(OWUI Settings > Integrations).",
            )
        refusal = await checker.check(api_key)
        if refusal is not None:
            return None, refusal
        return api_key, None

    async def openapi(request: Request) -> JSONResponse:
        _, refusal = await authorize(request)
        if refusal is not None:
            return refusal
        return JSONResponse(build_openapi(tools(), "Freshservice"))

    async def call(request: Request) -> JSONResponse:
        name = request.path_params["tool"]
        tool = tool_manager.get_tool(name)
        if tool is None:
            # Unregistered includes every disabled write tool.
            return _error(404, f"No tool named {name!r}.")

        api_key, refusal = await authorize(request)
        if refusal is not None:
            return refusal

        raw = await request.body()
        if raw.strip():
            try:
                arguments = json.loads(raw)
            except ValueError:
                return _error(422, "The request body must be a JSON object.")
        else:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(422, "The request body must be a JSON object.")

        token = _request_api_key.set(api_key)
        try:
            result = await tool.run(arguments)
        except Exception as exc:
            if isinstance(exc.__cause__, ValidationError):
                problems = "; ".join(
                    f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
                    for err in exc.__cause__.errors(include_url=False, include_input=False)
                )
                return _error(422, f"Invalid arguments for {name}: {problems}")
            log.warning("TOOL_FAILED tool=%s error=%s", name, type(exc).__name__)
            return _error(500, f"{name} failed: {type(exc).__name__}")
        finally:
            _request_api_key.reset(token)

        return JSONResponse(result)

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/openapi.json", openapi, methods=["GET"]),
            Route("/{tool}", call, methods=["POST"]),
        ]
    )
