"""Builds the MCP server and the ASGI app that serves it over streamable HTTP."""

from __future__ import annotations

import hmac
import logging

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
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


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on everything except the health check."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == HEALTH_PATH:
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


def _advertise_public_clients(app: Starlette, auth: AuthSettings) -> None:
    """Say in the server metadata that clients without a secret are welcome.

    The SDK accepts ``token_endpoint_auth_method: none`` at registration but
    leaves it out of the metadata it publishes; clients that check first would
    conclude they cannot connect.
    """
    metadata = build_metadata(
        auth.issuer_url,  # as the SDK parsed it: no trailing slash added
        None,
        ClientRegistrationOptions(enabled=True),
        RevocationOptions(enabled=True),
    )
    metadata.token_endpoint_auth_methods_supported = [
        "none",
        *(metadata.token_endpoint_auth_methods_supported or []),
    ]
    metadata.revocation_endpoint_auth_methods_supported = [
        "none",
        *(metadata.revocation_endpoint_auth_methods_supported or []),
    ]
    for route in app.routes:
        if getattr(route, "path", None) == "/.well-known/oauth-authorization-server":
            route.app = cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"])
            return
    raise RuntimeError("authorization server metadata route not found")


def build_oauth_provider(settings: Settings) -> GarminOAuthProvider:
    assert settings.public_url and settings.resource_url
    provider = GarminOAuthProvider(
        OAuthStore(settings.oauth_db),
        issuer=settings.public_url,
        resource_url=settings.resource_url,
        static_token=settings.auth_token,
        access_ttl=settings.oauth_access_ttl,
        refresh_ttl=settings.oauth_refresh_ttl,
    )
    if provider.store.password_hash() is None:
        logger.warning(
            "OAuth is enabled but no owner password is set, so no client can be authorized. "
            "Run `garmin-mcp set-password` (installed service: `sudo garmin-mcp set-password`)."
        )
    return provider


def build_server(settings: Settings, garmin: GarminSession | None = None) -> MCPServer:
    garmin = garmin or GarminSession(settings)
    oauth: dict = {}
    provider = None
    if settings.oauth_enabled:
        provider = build_oauth_provider(settings)
        oauth = {
            "auth_server_provider": provider,
            "auth": AuthSettings(
                issuer_url=settings.public_url,
                resource_server_url=settings.resource_url,
                # Tokens are bound to this endpoint; refuse any that are not.
                validate_token_resource=True,
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
        # The SDK's bearer middleware guards the MCP endpoint. It accepts OAuth
        # access tokens and, if configured, GARMIN_MCP_AUTH_TOKEN (see
        # GarminOAuthProvider.load_access_token).
        _advertise_public_clients(app, mcp.settings.auth)
        app.add_middleware(RevokeCompatMiddleware)
        logger.info(
            "OAuth enabled: public endpoint %s, sign-in at %s",
            settings.resource_url,
            settings.public_url,
        )
    elif settings.auth_token:
        app.add_middleware(BearerAuthMiddleware, token=settings.auth_token)
    else:
        logger.warning(
            "GARMIN_MCP_AUTH_TOKEN is not set: anyone who can reach %s:%s can read "
            "this Garmin account's data.",
            settings.host,
            settings.port,
        )
    return app
