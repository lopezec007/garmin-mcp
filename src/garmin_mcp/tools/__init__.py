"""Tool modules. Each one exposes ``register(mcp, garmin)``."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..session import GarminSession
from . import activities, body, health, performance, profile, training

MODULES = (profile, activities, health, performance, body, training)


def register_all(mcp: MCPServer, garmin: GarminSession) -> None:
    for module in MODULES:
        module.register(mcp, garmin)
