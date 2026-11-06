"""GarminSession: login, token dir, cooldown, re-login, error mapping, dates."""

from __future__ import annotations

from dataclasses import replace

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError

import garmin_mcp.session as session_mod
from garmin_mcp.session import GarminLoginError, GarminSession


class StubGarmin:
    """Stands in for garminconnect.Garmin; behaviour is scripted per test."""

    instances: list[StubGarmin] = []
    login_errors: list[Exception | None] = []
    needs_mfa = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tokenstore = None
        self.results: list = []
        StubGarmin.instances.append(self)

    def login(self, tokenstore):
        self.tokenstore = tokenstore
        if StubGarmin.needs_mfa:
            self.mfa_code = self.kwargs["prompt_mfa"]()
        error = StubGarmin.login_errors.pop(0) if StubGarmin.login_errors else None
        if error:
            raise error
        return None, None

    def get_full_name(self):
        return "Stub User"

    def get_thing(self, *args):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def stub(monkeypatch):
    StubGarmin.instances = []
    StubGarmin.login_errors = []
    StubGarmin.needs_mfa = False
    monkeypatch.setattr(session_mod, "Garmin", StubGarmin)
    return StubGarmin


def test_login_uses_credentials_and_token_dir(settings):
    api = GarminSession(settings).login_sync()
    assert api.kwargs["email"] == "me@example.com"
    assert api.kwargs["password"] == "pw"
    assert api.tokenstore == str(settings.token_dir)
    assert settings.token_dir.is_dir()


def test_login_is_cached(settings, stub):
    session = GarminSession(settings)
    assert session.login_sync() is session.login_sync()
    assert len(stub.instances) == 1


def test_service_login_cannot_answer_mfa(settings, stub):
    stub.needs_mfa = True
    with pytest.raises(GarminLoginError, match="MFA"):
        GarminSession(settings).login_sync()


def test_interactive_login_passes_mfa_prompt(settings, stub):
    stub.needs_mfa = True
    api = GarminSession(settings).login_sync(prompt_mfa=lambda: "123456", force=True)
    assert api.mfa_code == "123456"


def test_failed_login_is_not_retried_during_cooldown(settings, stub):
    stub.login_errors = [GarminConnectAuthenticationError("bad password")]
    session = GarminSession(settings)
    with pytest.raises(GarminLoginError, match="bad password"):
        session.login_sync()
    with pytest.raises(GarminLoginError, match="not retrying"):
        session.login_sync()
    assert len(stub.instances) == 1  # Garmin was contacted only once


def test_failed_login_is_retried_after_cooldown(settings, stub):
    stub.login_errors = [GarminConnectTooManyRequestsError("429")]
    session = GarminSession(replace(settings, login_retry_cooldown=0))
    with pytest.raises(GarminLoginError, match="rate-limiting"):
        session.login_sync()
    assert session.login_sync().get_full_name() == "Stub User"


def test_call_relogs_in_once_when_session_is_rejected(settings, stub):
    session = GarminSession(settings)
    first = session.login_sync()
    first.results = [GarminConnectAuthenticationError("expired")]
    original_new_client = session._new_client

    def new_client(prompt_mfa=None):
        api = original_new_client(prompt_mfa)
        api.results = ["fresh"]
        return api

    session._new_client = new_client
    assert session.call_sync("get_thing", 1) == "fresh"
    assert len(stub.instances) == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GarminConnectNotFoundError("404"), "no such resource"),
        (GarminConnectTooManyRequestsError("429"), "rate limit"),
        (GarminConnectConnectionError("boom"), "request failed"),
        (ValueError("date must be YYYY-MM-DD"), "Invalid argument"),
        (KeyError("userProfilePK"), "Unexpected error from the Garmin library"),
    ],
)
def test_call_maps_library_errors_to_tool_errors(settings, error, expected):
    session = GarminSession(settings)
    session.login_sync().results = [error]
    with pytest.raises(ToolError, match=expected):
        session.call_sync("get_thing")


def test_call_reports_login_failure_as_tool_error(settings, stub):
    stub.login_errors = [GarminConnectAuthenticationError("bad password")]
    with pytest.raises(ToolError, match="garmin-mcp login"):
        GarminSession(settings).call_sync("get_thing")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "2026-03-15"),
        ("", "2026-03-15"),
        ("today", "2026-03-15"),
        ("Yesterday", "2026-03-14"),
        ("7d", "2026-03-08"),
        ("-7d", "2026-03-08"),
        ("+7d", "2026-03-22"),
        ("tomorrow", "2026-03-16"),
        ("2025-12-31", "2025-12-31"),
    ],
)
async def test_date(garmin, value, expected):
    assert await garmin.date(value) == expected


async def test_date_rejects_garbage(garmin):
    with pytest.raises(ToolError, match="Invalid date"):
        await garmin.date("last tuesday")


async def test_date_range(garmin):
    assert await garmin.date_range(None, None, default_days=7) == ("2026-03-09", "2026-03-15")
    assert await garmin.date_range("30d", "yesterday") == ("2026-02-13", "2026-03-14")
    with pytest.raises(ToolError, match="after end date"):
        await garmin.date_range("today", "yesterday")
