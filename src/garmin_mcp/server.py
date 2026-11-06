"""Builds the MCP server and the ASGI app that serves it over streamable HTTP."""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .login import make_login_endpoint
from .oauth import LOGIN_PATH, GarminOAuthProvider, OAuthStore
from .session import GarminSession
from .tools import register_all

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to the owner's personal Garmin Connect account: activities, \
daily health and wellness (sleep, heart rate, stress, Body Battery, HRV, steps), \
training and performance metrics, body composition, workouts, devices and gear.

Dates are YYYY-MM-DD (or 'today', 'yesterday', '<N>d' for N days ago) in the \
account's timezone. Call get_user_profile first to learn the unit system. \
Today's data is only as fresh as the watch's last sync.
"""

HEALTH_PATH = "/healthz"


WELL_KNOWN_PREFIX = "/.well-known/"


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` (the mode without OAuth).

    Everything is guarded except the health check and ``/.well-known/``. The
    latter serves nothing in this mode, and must say so honestly: clients
    probe those paths to find out whether a server offers OAuth, and a 401
    there reads as "there is OAuth metadata, but you may not see it" instead
    of the true answer, a 404.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] == HEALTH_PATH
            or scope["path"].startswith(WELL_KNOWN_PREFIX)
        ):
            await self.app(scope, receive, send)
            return
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RevokeCompatMiddleware:
    """Let public clients use ``/revoke`` as RFC 7009 describes it.

    The SDK's revocation handler requires a ``client_secret`` form field even
    from clients that have no secret, so a public client's "disconnect" fails
    with 400 and its tokens stay valid. Add an empty one when it is missing.
    """

    MAX_BODY = 16 * 1024

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/revoke" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        content_type = headers.get(b"content-type", b"").split(b";")[0].strip().lower()
        if content_type != b"application/x-www-form-urlencoded":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return  # client went away
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.MAX_BODY:
                response = JSONResponse({"error": "invalid_request"}, status_code=413)
                await response(scope, receive, send)
                return
        if not any(pair.split(b"=", 1)[0] == b"client_secret" for pair in body.split(b"&")):
            body += b"&client_secret=" if body else b"client_secret="
        scope = dict(scope)
        scope["headers"] = [
            (name, value) for name, value in scope["headers"] if name != b"content-length"
        ] + [(b"content-length", str(len(body)).encode())]
        sent = False

        async def replay() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# Stand-in origin handed to the SDK when no public URL is configured. The SDK
# wants a fixed https issuer at start-up; everything it would publish under
# this name is rewritten per request to the address the client actually used.
PLACEHOLDER_ORIGIN = "https://garmin-mcp.invalid"

RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"

_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253})|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$"
)

OriginOf = Callable[[HTTPConnection], "str | None"]


def make_origin_of(settings: Settings) -> OriginOf:
    """How this server names itself to a given client: ``scheme://host[:port]``.

    With GARMIN_MCP_PUBLIC_URL set, always that. Otherwise the address the
    request was sent to (its Host header, and the scheme as seen by uvicorn,
    which honours X-Forwarded-Proto from a trusted proxy), so the same server
    answers correctly as http://192.168.1.10:8765, http://garmin.lan:8765 or
    behind a proxy -- with nothing to configure. None if the request has no
    usable Host header.
    """
    if settings.public_url:
        fixed = settings.public_url
        return lambda _conn: fixed

    def origin_of(conn: HTTPConnection) -> str | None:
        host = conn.headers.get("host", "")
        if not _HOST_RE.fullmatch(host):
            return None
        return f"{conn.scope.get('scheme', 'http')}://{host.lower()}"

    return origin_of


def _rebase(value: Any, old_origin: str, new_origin: str) -> Any:
    """Replace ``old_origin`` at the start of every URL string in a JSON document."""
    if isinstance(value, str):
        if value == old_origin or value.startswith(old_origin + "/"):
            return new_origin + value[len(old_origin) :]
        return value
    if isinstance(value, list):
        return [_rebase(item, old_origin, new_origin) for item in value]
    if isinstance(value, dict):
        return {key: _rebase(item, old_origin, new_origin) for key, item in value.items()}
    return value


def _metadata_endpoint(document: dict[str, Any], template_origin: str, origin_of: OriginOf):
    """Serve a discovery document with its URLs based on the caller's view of us."""

    async def endpoint(request: Request) -> JSONResponse:
        origin = origin_of(request)
        if origin is None:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "missing or invalid Host header"},
                status_code=400,
            )
        return JSONResponse(
            _rebase(document, template_origin, origin),
            # The content depends on the address used, so it must not be
            # cached and replayed for a different one.
            headers={"Cache-Control": "no-store"},
        )

    return cors_middleware(endpoint, ["GET", "OPTIONS"])


class RequireBearer:
    """Guard the MCP endpoint: let authenticated requests through, challenge the rest.

    Same job as the SDK's RequireAuthMiddleware, except that the challenge
    points at the discovery document under the address the client used,
    which the SDK can only do for one address fixed at start-up.
    """

    def __init__(self, app: ASGIApp, origin_of: OriginOf, mcp_path: str) -> None:
        self.app = app
        self._origin_of = origin_of
        self._metadata_path = RESOURCE_METADATA_PATH + mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if isinstance(scope.get("user"), AuthenticatedUser):
            await self.app(scope, receive, send)
            return
        challenge = ['error="invalid_token"', 'error_description="Authentication required"']
        origin = self._origin_of(HTTPConnection(scope))
        if origin is not None:
            challenge.append(f'resource_metadata="{origin}{self._metadata_path}"')
        response = JSONResponse(
            {"error": "invalid_token", "error_description": "Authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer " + ", ".join(challenge)},
        )
        await response(scope, receive, send)


def _route(app: Starlette, path: str) -> Route:
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route  # type: ignore[return-value]
    raise RuntimeError(f"route {path} not found")


def _install_oauth_routes(app: Starlette, settings: Settings, auth: AuthSettings) -> None:
    """Adapt the routes the SDK built so they work under any address.

    The SDK bakes one issuer URL into the two discovery documents and into
    the 401 challenge. Replace those three with versions that use the
    address of the request (or the configured public URL), and fill two
    gaps while at it.
    """
    origin_of = make_origin_of(settings)
    template_origin = str(auth.issuer_url)  # no trailing slash: see AuthSettings

    server = build_metadata(
        auth.issuer_url,
        None,
        ClientRegistrationOptions(enabled=True),
        RevocationOptions(enabled=True),
    )
    # The SDK accepts clients without a secret but does not say so in the
    # metadata; clients that check first would conclude they cannot connect.
    server.token_endpoint_auth_methods_supported = [
        "none",
        *(server.token_endpoint_auth_methods_supported or []),
    ]
    server.revocation_endpoint_auth_methods_supported = [
        "none",
        *(server.revocation_endpoint_auth_methods_supported or []),
    ]
    _route(app, SERVER_METADATA_PATH).app = _metadata_endpoint(
        server.model_dump(mode="json", exclude_none=True), template_origin, origin_of
    )

    resource = ProtectedResourceMetadata(
        resource=auth.resource_server_url,
        authorization_servers=[auth.issuer_url],
    )
    resource_endpoint = _metadata_endpoint(
        resource.model_dump(mode="json", exclude_none=True), template_origin, origin_of
    )
    _route(app, RESOURCE_METADATA_PATH + settings.path).app = resource_endpoint
    # RFC 9728 puts the document for https://host/mcp at .../oauth-protected-
    # resource/mcp. Clients that probe the bare path first should find it too.
    app.router.routes.append(
        Route(RESOURCE_METADATA_PATH, endpoint=resource_endpoint, methods=["GET", "OPTIONS"])
    )

    mcp_route = _route(app, settings.path)
    mcp_route.app = RequireBearer(mcp_route.app.app, origin_of, settings.path)  # type: ignore[attr-defined]


def build_oauth_provider(settings: Settings) -> GarminOAuthProvider:
    provider = GarminOAuthProvider(
        OAuthStore(settings.oauth_db),
        issuer=settings.public_url,
        mcp_path=settings.path,
        static_token=settings.auth_token,
        access_ttl=settings.oauth_access_ttl,
        refresh_ttl=settings.oauth_refresh_ttl,
    )
    if provider.store.password_hash() is None:
        hint = "Run `garmin-mcp set-password` (installed service: `sudo garmin-mcp set-password`)."
        if settings.auth_token is None:
            logger.warning(
                "No owner password and no GARMIN_MCP_AUTH_TOKEN are set, so nothing can "
                "connect yet. %s",
                hint,
            )
        elif settings.public_url:
            logger.warning(
                "No owner password is set, so no client can sign in with OAuth. %s", hint
            )
        else:
            logger.info("OAuth sign-in becomes available once an owner password is set. %s", hint)
    return provider


def build_server(settings: Settings, garmin: GarminSession | None = None) -> MCPServer:
    garmin = garmin or GarminSession(settings)
    oauth: dict = {}
    provider = None
    if settings.oauth_enabled:
        provider = build_oauth_provider(settings)
        origin = settings.public_url or PLACEHOLDER_ORIGIN
        oauth = {
            "auth_server_provider": provider,
            "auth": AuthSettings(
                issuer_url=origin,
                resource_server_url=f"{origin}{settings.path}",
                # With a fixed public address, insist that tokens name it.
                # Without one there is no address to insist on; tokens are
                # opaque and checked against this server's own database
                # either way.
                validate_token_resource=settings.public_url is not None,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
            ),
        }
    mcp = MCPServer(
        name="garmin",
        title="Garmin Connect",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level=settings.log_level,  # type: ignore[arg-type]
        **oauth,
    )
    register_all(mcp, garmin)

    if provider is not None:
        mcp.custom_route(LOGIN_PATH, methods=["GET", "POST"], include_in_schema=False)(
            make_login_endpoint(provider, LOGIN_PATH)
        )

    @mcp.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    return mcp


def build_app(settings: Settings, garmin: GarminSession | None = None) -> Starlette:
    mcp = build_server(settings, garmin)
    if settings.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
        )
    else:
        # A LAN service is reached by IP, hostname or mDNS name; without an
        # explicit allowlist there is no Host value we could sensibly pin.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    # Stateless JSON mode: every request stands alone, so clients on several
    # devices keep working across service restarts with no session to lose.
    app = mcp.streamable_http_app(
        streamable_http_path=settings.path,
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host=settings.host,
    )
    if settings.oauth_enabled:
        # The SDK's bearer middleware authenticates requests: it accepts OAuth
        # access tokens and, if configured, GARMIN_MCP_AUTH_TOKEN (see
        # GarminOAuthProvider.load_access_token).
        _install_oauth_routes(app, settings, mcp.settings.auth)
        app.add_middleware(RevokeCompatMiddleware)
        logger.info(
            "OAuth sign-in enabled; clients are told to use %s",
            settings.public_url or "the address they connected to",
        )
    elif settings.auth_token:
        app.add_middleware(BearerAuthMiddleware, token=settings.auth_token)
    else:
        logger.warning(
            "OAuth is off and GARMIN_MCP_AUTH_TOKEN is not set: anyone who can reach %s:%s "
            "can read this Garmin account's data.",
            settings.host,
            settings.port,
        )
    return app
