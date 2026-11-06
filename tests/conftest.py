"""Shared fixtures: a fake ``garminconnect.Garmin`` and an in-memory MCP client.

``FakeGarmin`` answers any method call. By default it returns ``{}``; tests set
canned payloads with ``fake.responses["get_sleep_data"] = {...}`` (a value, or a
callable receiving the call's args/kwargs) and inspect ``fake.calls`` afterwards.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from mcp import Client

from garmin_mcp.config import Settings
from garmin_mcp.server import build_server
from garmin_mcp.session import GarminSession

TODAY = date(2026, 3, 15)


class FakeGarmin:
    def __init__(self) -> None:
        self.responses: dict[str, Any] = {
            "get_full_name": "Test User",
            "get_unit_system": "metric",
            "get_user_timezone": "Europe/Madrid",
        }
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def today(self) -> date:
        return TODAY

    def calls_to(self, name: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            response = self.responses.get(name, {})
            if isinstance(response, Exception):
                raise response
            return response(*args, **kwargs) if callable(response) else response

        return method


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    # OAuth off by default here: most tests are about the tools, and the ones
    # about the HTTP layer switch it on explicitly.
    return Settings(
        email="me@example.com",
        password="pw",
        token_dir=tmp_path / "tokens",
        oauth=False,
        oauth_db=tmp_path / "oauth.db",
    )


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture
def garmin(settings: Settings, fake: FakeGarmin) -> GarminSession:
    session = GarminSession(settings)
    session._api = fake  # type: ignore[assignment]  # already "logged in"
    return session


@pytest.fixture
async def client(settings: Settings, garmin: GarminSession):
    async with Client(build_server(settings, garmin)) as c:
        yield c


async def call(client: Client, tool: str, **arguments: Any) -> Any:
    """Call a tool and return its decoded JSON result; raises if the tool errored."""
    result = await client.call_tool(tool, arguments)
    text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    if result.is_error:
        raise AssertionError(f"{tool} failed: {text}")
    assert len(result.content) == 1, "tools must return a single JSON text block (use dump())"
    return json.loads(text)


async def call_error(client: Client, tool: str, **arguments: Any) -> str:
    """Call a tool that is expected to fail and return its error text."""
    result = await client.call_tool(tool, arguments)
    assert result.is_error, f"{tool} unexpectedly succeeded"
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))
