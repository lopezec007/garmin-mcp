"""Builds the MCP server and the ASGI app that serves it over streamable HTTP."""

from __future__ import annotations

import hmac
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
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


def build_server(settings: Settings, garmin: GarminSession | None = None) -> MCPServer:
    garmin = garmin or GarminSession(settings)
    mcp = MCPServer(
        name="garmin",
        title="Garmin Connect",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level=settings.log_level,  # type: ignore[arg-type]
    )
    register_all(mcp, garmin)

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
    if settings.auth_token:
        app.add_middleware(BearerAuthMiddleware, token=settings.auth_token)
    else:
        logger.warning(
            "GARMIN_MCP_AUTH_TOKEN is not set: anyone who can reach %s:%s can read "
            "this Garmin account's data.",
            settings.host,
            settings.port,
        )
    return app
