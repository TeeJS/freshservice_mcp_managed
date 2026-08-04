"""OAuth 2.1 resource-server support for the Freshservice managed MCP server.

This module turns the MCP HTTP endpoint into a protected resource:

    unauthenticated:  /healthz
                      /.well-known/oauth-protected-resource[/mcp]
                      /.well-known/oauth-authorization-server
          gated:      /mcp   -> validate bearer token, resolve permission,
                               attach it to the ASGI scope

Discovery and health deliberately stay outside the gate: gating them makes the
authorization flow undiscoverable and breaks the container healthcheck the
moment auth is switched on.

Permission travels on the ASGI scope rather than a contextvar because the MCP
request handler runs in a different task than this middleware.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
import jwt
from jwt import PyJWKClient
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

log = logging.getLogger(__name__)

# Key under which the resolved permission is stashed on the ASGI scope.
SCOPE_KEY = "mcp_permission"

# Permission levels, in increasing order of privilege.
PERM_NONE = "none"
PERM_READ = "read"
PERM_WRITE = "write"

# Scopes this resource advertises. A client that is not told which scopes to
# request falls back to requesting every scope the authorization server
# advertises; providers that reject (rather than ignore) a scope the client may
# not request then fail the whole authorization request. Declaring them here and
# on the 401 challenge is what prevents that.
SCOPES = (
    "openid",
    "profile",
    "email",
    "address",
    "phone",
    "groups",
    "offline_access",
)

# How long a resolved JWKS client is reused before rediscovery.
JWKS_TTL_SECONDS = 3600


def _env(name: str, default: str = "") -> str:
    """Read an env var and strip surrounding whitespace.

    These values get pasted by hand into container management UIs, and an
    invisible leading tab on the issuer produces an unusable discovery URL with
    an error that points nowhere near the real mistake.
    """
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_set(name: str) -> frozenset[str]:
    return frozenset(
        part.strip() for part in _env(name).split(",") if part.strip()
    )


@dataclass(frozen=True)
class OAuthConfig:
    """Resource-server configuration, normalized at load time."""

    enabled: bool = False
    allow_insecure: bool = False
    issuer: str = ""
    server_url: str = ""
    mcp_path: str = "/mcp"
    audience: str = ""
    groups_claim: str = "groups"
    read_groups: frozenset[str] = field(default_factory=frozenset)
    write_groups: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "OAuthConfig":
        return cls(
            enabled=_env_bool("MCP_OAUTH_ENABLED", False),
            allow_insecure=_env_bool("MCP_ALLOW_INSECURE", False),
            issuer=_env("MCP_OAUTH_ISSUER").rstrip("/"),
            server_url=_env("MCP_SERVER_URL").rstrip("/"),
            mcp_path="/" + _env("MCP_PATH", "/mcp").strip("/"),
            audience=_env("MCP_OAUTH_AUDIENCE"),
            groups_claim=_env("MCP_OAUTH_GROUPS_CLAIM", "groups"),
            read_groups=_env_set("MCP_READ_GROUPS"),
            write_groups=_env_set("MCP_WRITE_GROUPS"),
        )

    @property
    def resource(self) -> str:
        """The canonical resource identifier.

        This must equal the connector URL the user enters, path included. If the
        connector is https://host/mcp then the resource is https://host/mcp, not
        https://host.
        """
        return f"{self.server_url}{self.mcp_path}"

    @property
    def protected_resource_path(self) -> str:
        return "/.well-known/oauth-protected-resource"

    @property
    def metadata_url(self) -> str:
        return f"{self.server_url}{self.protected_resource_path}{self.mcp_path}"

    @property
    def has_group_policy(self) -> bool:
        return bool(self.read_groups or self.write_groups)

    def validate(self) -> list[str]:
        """Return a list of configuration problems, empty when usable."""
        problems: list[str] = []
        if not self.enabled:
            return problems
        if not self.issuer:
            problems.append("MCP_OAUTH_ISSUER is required when MCP_OAUTH_ENABLED=true")
        elif not self.issuer.startswith("https://"):
            problems.append(f"MCP_OAUTH_ISSUER must be https, got {self.issuer!r}")
        if not self.server_url:
            problems.append("MCP_SERVER_URL is required when MCP_OAUTH_ENABLED=true")
        elif not self.server_url.startswith("https://"):
            problems.append(f"MCP_SERVER_URL must be https, got {self.server_url!r}")
        return problems


class ResourceServer:
    """Validates access tokens and maps group membership onto a permission."""

    def __init__(self, config: OAuthConfig) -> None:
        self.config = config
        self._jwks: PyJWKClient | None = None
        self._jwks_resolved_at: float = 0.0
        self._as_metadata: dict[str, Any] | None = None

    # --- discovery -------------------------------------------------------

    def _discovery_document(self) -> dict[str, Any]:
        """Fetch and cache the issuer's OpenID configuration."""
        if self._as_metadata is not None:
            return self._as_metadata
        url = f"{self.config.issuer}/.well-known/openid-configuration"
        doc = httpx.get(url, timeout=10).json()
        self._as_metadata = doc
        return doc

    def _jwks_client(self) -> PyJWKClient:
        """Resolve discovery lazily and cache it.

        Lazy resolution lets this server start before the identity provider when
        both are containers coming up in an arbitrary order. On a refresh
        failure a stale client is preferable to refusing every request.
        """
        fresh = (
            self._jwks is not None
            and time.monotonic() - self._jwks_resolved_at < JWKS_TTL_SECONDS
        )
        if fresh:
            return self._jwks  # type: ignore[return-value]
        try:
            doc = self._discovery_document()
            self._jwks = PyJWKClient(doc["jwks_uri"], cache_keys=True)
            self._jwks_resolved_at = time.monotonic()
        except Exception:
            if self._jwks is not None:
                log.warning(
                    "OAUTH_DISCOVERY_REFRESH_FAILED issuer=%s (serving stale JWKS)",
                    self.config.issuer,
                )
                return self._jwks
            self._as_metadata = None
            raise
        return self._jwks

    # --- token validation ------------------------------------------------

    def verify(self, token: str) -> dict[str, Any]:
        """Validate a JWT access token and return its claims.

        Raises on any failure; callers must not leak the reason to the client
        beyond the generic invalid_token error.
        """
        signing_key = self._jwks_client().get_signing_key_from_jwt(token).key
        audience = self.config.audience or None
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=self.config.issuer,
            audience=audience,
            options={"verify_aud": bool(audience)},
        )

    # --- authorization ---------------------------------------------------

    def groups_from(self, claims: dict[str, Any]) -> set[str]:
        """Extract group names, tolerating the three shapes providers emit."""
        raw = claims.get(self.config.groups_claim)
        if raw is None:
            return set()
        if isinstance(raw, str):
            return {part for part in raw.split() if part}
        if isinstance(raw, Iterable):
            return {str(item) for item in raw}
        return set()

    def evaluate(self, groups: set[str]) -> str:
        """Map group membership onto a permission level.

        Returns "write", "read", or "none". With no group policy configured at
        all, any authenticated caller gets write — the token itself is then the
        control. Once either list is set, unmapped callers are denied.
        """
        if not self.config.has_group_policy:
            return PERM_WRITE
        if groups & self.config.write_groups:
            return PERM_WRITE
        if groups & self.config.read_groups:
            return PERM_READ
        return PERM_NONE


# --- responses -----------------------------------------------------------


def _www_authenticate(config: OAuthConfig, error: str = "", description: str = "") -> str:
    parts = [f'Bearer resource_metadata="{config.metadata_url}"']
    parts.append(f'scope="{" ".join(SCOPES)}"')
    if error:
        parts.append(f'error="{error}"')
    if description:
        parts.append(f'error_description="{description}"')
    return ", ".join(parts)


async def _send_json(send, status: int, payload: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if headers:
        raw_headers.extend(headers)
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": body})


# --- middleware ----------------------------------------------------------


class AuthMiddleware:
    """Pure ASGI middleware so the 401 is emitted before any MCP framing."""

    def __init__(self, app, resource_server: ResourceServer) -> None:
        self.app = app
        self.rs = resource_server
        self.config = resource_server.config

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(self.config.mcp_path):
            # Health and discovery stay open.
            await self.app(scope, receive, send)
            return

        token = self._bearer_token(scope)
        if not token:
            await _send_json(
                send,
                401,
                {
                    "error": "unauthorized",
                    "error_description": "a bearer access token is required",
                },
                [(b"www-authenticate", _www_authenticate(self.config).encode())],
            )
            return

        try:
            claims = self.rs.verify(token)
        except Exception as exc:
            # Log the reason, never the token.
            log.warning("OAUTH_TOKEN_REJECTED error=%s: %s", type(exc).__name__, exc)
            await _send_json(
                send,
                401,
                {
                    "error": "invalid_token",
                    "error_description": "the access token is not valid",
                },
                [
                    (
                        b"www-authenticate",
                        _www_authenticate(
                            self.config, "invalid_token", "the access token is not valid"
                        ).encode(),
                    )
                ],
            )
            return

        subject = claims.get("sub", "<unknown>")
        groups = self.rs.groups_from(claims)
        permission = self.rs.evaluate(groups)

        # Group names are logged deliberately. An empty list and a populated one
        # that simply misses the mapped group are different faults -- a missing
        # claims policy on the provider versus a missing group on the account --
        # and they are otherwise indistinguishable from the client side.
        groups_repr = ",".join(sorted(groups)) if groups else "<none in token>"

        if permission == PERM_NONE:
            # The token is valid; re-authenticating changes nothing, so 403.
            log.warning(
                "AUTHZ_DENIED subject=%s groups=%s reason=no_mapped_group "
                "expected_read=%s expected_write=%s",
                subject,
                groups_repr,
                ",".join(sorted(self.config.read_groups)) or "<unset>",
                ",".join(sorted(self.config.write_groups)) or "<unset>",
            )
            await _send_json(
                send,
                403,
                {
                    "error": "forbidden",
                    "error_description": "your account is not a member of a group "
                    "permitted to use this server",
                },
            )
            return

        # Info, not debug: who was granted what is an audit record, and it is
        # worthless if it only appears once someone thinks to raise the level.
        log.info(
            "AUTHZ_GRANTED subject=%s permission=%s groups=%s",
            subject,
            permission,
            groups_repr,
        )
        scope[SCOPE_KEY] = permission
        await self.app(scope, receive, send)

    @staticmethod
    def _bearer_token(scope) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() != b"authorization":
                continue
            raw = value.decode("latin-1").strip()
            if raw.lower().startswith("bearer "):
                return raw[7:].strip()
        return ""


# --- unauthenticated routes ---------------------------------------------


def discovery_routes(config: OAuthConfig, tool_counter) -> list[Route]:
    """Build the routes that must remain reachable without a token."""

    async def healthz(request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "service": "freshservice-mcp-managed",
                "auth": "oauth" if config.enabled else "none",
                "tools": tool_counter(),
            }
        )

    async def protected_resource(request: Request) -> Response:
        return JSONResponse(
            {
                "resource": config.resource,
                "authorization_servers": [config.issuer],
                "scopes_supported": list(SCOPES),
                "bearer_methods_supported": ["header"],
            }
        )

    async def authorization_server(request: Request) -> Response:
        """Mirror the issuer's discovery document.

        Clients probe this on the resource host, which matters when the issuer
        serves its metadata from a nonstandard path.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                upstream = await client.get(
                    f"{config.issuer}/.well-known/openid-configuration"
                )
            return JSONResponse(upstream.json(), status_code=upstream.status_code)
        except Exception as exc:
            log.warning("OAUTH_AS_MIRROR_FAILED issuer=%s error=%s", config.issuer, exc)
            return JSONResponse(
                {
                    "error": "upstream_unavailable",
                    "error_description": "could not reach the authorization server",
                },
                status_code=502,
            )

    routes = [Route("/healthz", healthz, methods=["GET"])]
    if config.enabled:
        pr = config.protected_resource_path
        routes.extend(
            [
                # Claude probes the path-suffixed form first.
                Route(f"{pr}{config.mcp_path}", protected_resource, methods=["GET"]),
                Route(pr, protected_resource, methods=["GET"]),
                Route(
                    "/.well-known/oauth-authorization-server",
                    authorization_server,
                    methods=["GET"],
                ),
            ]
        )
    return routes
