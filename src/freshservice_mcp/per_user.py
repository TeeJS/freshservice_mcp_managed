"""Per-user key mode: each Open WebUI user's calls run with their own Freshservice key.

In this mode the server holds no Freshservice credential. It is added in Open
WebUI as an MCP tool server with Auth set to "Session", so every request
carries the calling user's own Open WebUI login token. Before a tool runs, the
server uses that token to ask Open WebUI for the user's Freshservice key, which
the user saved in the user settings ("valves") of the Freshservice Key tool:

    GET {OWUI_URL}/api/v1/tools/id/freshservice_key/valves/user

Open WebUI answers with that user's own settings only. Every Freshservice call
made for the request then uses that key and no other.

    open:   /healthz
    MCP:    /mcp       (tools/call needs a login token and a saved key)
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import logging
import time
from typing import Any

import httpx
from mcp import types
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

MODE_SHARED = "shared"
MODE_PER_USER = "per-user"
MODES = (MODE_SHARED, MODE_PER_USER)

# The Open WebUI tool that holds each user's key, and the field within it.
# Must match owui/freshservice_key.py.
KEY_TOOL_ID = "freshservice_key"
KEY_FIELD = "freshservice_api_key"

# Where users save their key, as shown in every "add your key" message.
WHERE_TO_SAVE = "in a chat, open Controls > Valves > Tools > Freshservice Key"

# How long a user's key is reused before Open WebUI is asked again.
KEY_CACHE_TTL_SECONDS = 300
# Upper bound on remembered keys.
KEY_CACHE_MAX = 1000

# The key for the call being served. Set before a tool runs, read by
# get_auth_headers(); a contextvar keeps concurrent calls apart.
_request_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "freshservice_request_api_key", default=None
)


def current_api_key() -> str | None:
    """The Freshservice API key for the call being served, if any."""
    return _request_api_key.get()


def basic_auth_value(api_key: str) -> str:
    """Freshservice takes the API key as the Basic-auth username, password X."""
    return "Basic " + base64.b64encode(f"{api_key}:X".encode()).decode()


class KeyNotAvailable(Exception):
    """The user's key could not be obtained. The message is shown to the user."""


def _bearer_token(request: Request | None) -> str | None:
    if request is None:
        return None
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


class KeyLookup:
    """Fetch a user's Freshservice key from Open WebUI with their login token.

    Found keys are remembered for a few minutes, in memory only, under a
    SHA-256 hash of the login token. Neither the token nor the key is logged.
    """

    def __init__(self, owui_url: str) -> None:
        self.url = f"{owui_url.rstrip('/')}/api/v1/tools/id/{KEY_TOOL_ID}/valves/user"
        self._cache: dict[str, tuple[str, float]] = {}

    def _cached(self, digest: str) -> str | None:
        entry = self._cache.get(digest)
        if entry is None:
            return None
        api_key, expires = entry
        if expires < time.monotonic():
            del self._cache[digest]
            return None
        return api_key

    def _remember(self, digest: str, api_key: str) -> None:
        now = time.monotonic()
        if len(self._cache) >= KEY_CACHE_MAX:
            self._cache = {d: e for d, e in self._cache.items() if e[1] >= now}
            if len(self._cache) >= KEY_CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
        self._cache[digest] = (api_key, now + KEY_CACHE_TTL_SECONDS)

    async def key_for(self, token: str | None) -> str:
        """The caller's Freshservice key, or KeyNotAvailable saying what to do."""
        if not token:
            log.warning("KEY_LOOKUP result=no_login_token")
            raise KeyNotAvailable(
                "Open WebUI didn't send a login token. In Open WebUI's admin settings, "
                "this tool server's Auth must be set to Session."
            )

        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cached(digest)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self.url, headers={"Authorization": f"Bearer {token}"}, timeout=10
                )
        except httpx.HTTPError as exc:
            log.warning("KEY_LOOKUP result=owui_unreachable error=%s", type(exc).__name__)
            raise KeyNotAvailable("Couldn't reach Open WebUI to read your Freshservice key. Try again.")

        if response.status_code == 404:
            log.warning("KEY_LOOKUP result=key_tool_missing")
            raise KeyNotAvailable(
                "The Freshservice Key tool isn't installed in Open WebUI "
                "(Workspace > Tools, ID freshservice_key). Ask your Open WebUI admin."
            )
        if response.status_code in (401, 403):
            log.info("KEY_LOOKUP result=refused status=%s", response.status_code)
            raise KeyNotAvailable(
                "Open WebUI wouldn't share your Freshservice key. Your login may have "
                "expired, or you don't have access to the Freshservice Key tool."
            )
        if response.status_code != 200:
            log.warning("KEY_LOOKUP result=unexpected status=%s", response.status_code)
            raise KeyNotAvailable("Couldn't read your Freshservice key from Open WebUI. Try again.")

        try:
            valves = response.json() or {}
        except ValueError:
            valves = {}
        api_key = str(valves.get(KEY_FIELD) or "").strip() if isinstance(valves, dict) else ""

        # Only an empty key is refused here. Freshservice doesn't document its
        # key format, so whether a key is valid is left to Freshservice.
        if not api_key:
            log.info("KEY_LOOKUP result=not_set")
            raise KeyNotAvailable(f"Add your Freshservice API key first: {WHERE_TO_SAVE}.")

        self._remember(digest, api_key)
        log.info("KEY_LOOKUP result=found")
        return api_key


def install_key_resolution(mcp, owui_url: str) -> None:
    """Resolve the caller's key before every tool call; refuse the call without one.

    Wraps the tools/call handler FastMCP already installed, the same way the
    shared mode's tool gating does. The key is set for the duration of the call
    only, so a tool can never run with a key that isn't the caller's.
    """
    lookup = KeyLookup(owui_url)
    srv = mcp._mcp_server
    original_call = srv.request_handlers[types.CallToolRequest]

    async def call_with_user_key(req):
        try:
            request = srv.request_context.request
        except LookupError:
            request = None

        try:
            api_key = await lookup.key_for(_bearer_token(request))
        except KeyNotAvailable as exc:
            return types.ServerResult(
                types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(exc))],
                    isError=True,
                )
            )

        token = _request_api_key.set(api_key)
        try:
            return await original_call(req)
        finally:
            _request_api_key.reset(token)

    srv.request_handlers[types.CallToolRequest] = call_with_user_key


def healthz_route(tool_count) -> Route:
    """Container healthcheck. Needs no token and reveals nothing sensitive."""

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "mode": MODE_PER_USER, "tools": tool_count()})

    return Route("/healthz", healthz, methods=["GET"])
