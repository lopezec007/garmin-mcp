"""The ASGI app: health check, bearer auth, host allowlist, MCP over real HTTP."""

from __future__ import annotations

import json
from dataclasses import replace

from starlette.testclient import TestClient

from garmin_mcp.config import Settings
from garmin_mcp.server import build_app, build_server
from garmin_mcp.tools._common import clean, dump

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def rpc(http: TestClient, method: str, params: dict | None = None, **kwargs):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {**MCP_HEADERS, **kwargs.pop("headers", {})}
    return http.post("/mcp", json=body, headers=headers, **kwargs)


def test_healthz_needs_no_auth(settings, garmin):
    app = build_app(replace(settings, auth_token="s3cret"), garmin)
    with TestClient(app) as http:
        response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_bearer_token_is_enforced(settings, garmin):
    app = build_app(replace(settings, auth_token="s3cret"), garmin)
    with TestClient(app) as http:
        assert rpc(http, "tools/list").status_code == 401
        assert rpc(http, "tools/list", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = rpc(http, "tools/list", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert ok.json()["result"]["tools"]


def test_tool_call_over_http(settings, garmin, fake):
    fake.responses["get_device_last_used"] = {"lastUsedDeviceName": "Forerunner", "x": None}
    with TestClient(build_app(settings, garmin)) as http:
        response = rpc(http, "tools/call", {"name": "get_device_last_used", "arguments": {}})
    result = response.json()["result"]
    assert not result.get("isError")
    assert json.loads(result["content"][0]["text"]) == {"lastUsedDeviceName": "Forerunner"}


def test_host_allowlist(settings, garmin):
    allowlisted = replace(settings, allowed_hosts=["garmin.lan:*"])
    # One app per TestClient: the MCP session manager's lifespan runs only once.
    with TestClient(build_app(allowlisted, garmin), base_url="http://garmin.lan:8765") as http:
        assert rpc(http, "tools/list").status_code == 200
    with TestClient(build_app(allowlisted, garmin), base_url="http://evil.example:8765") as http:
        assert rpc(http, "tools/list").status_code in (400, 403, 421)


async def test_every_tool_is_read_only_and_described(settings, garmin):
    tools = await build_server(settings, garmin).list_tools()
    assert len(tools) == len({t.name for t in tools})
    for tool in tools:
        assert tool.annotations and tool.annotations.read_only_hint, tool.name
        assert tool.description and len(tool.description) > 20, tool.name
        assert tool.output_schema is None, f"{tool.name}: use @mcp.tool(**READ) and return dump()"


def test_settings_from_env(monkeypatch, tmp_path):
    for key, value in {
        "GARMIN_EMAIL": "a@b.c",
        "GARMIN_PASSWORD": "pw",
        "GARMIN_MCP_PORT": "9000",
        "GARMIN_MCP_PATH": "garmin",
        "GARMIN_MCP_ALLOWED_HOSTS": "a:*, b:*",
        "GARMIN_MCP_AUTH_TOKEN": "",
        "STATE_DIRECTORY": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GARMIN_TOKEN_DIR", raising=False)
    s = Settings.from_env()
    assert (s.email, s.port, s.path) == ("a@b.c", 9000, "/garmin")
    assert s.allowed_hosts == ["a:*", "b:*"]
    assert s.auth_token is None
    assert s.token_dir == tmp_path / "tokens"
    assert "pw" not in repr(s)


def test_clean_and_dump():
    payload = {"a": None, "b": 0, "c": False, "d": [], "e": {"f": None}, "g": [{"h": None, "i": 1}]}
    assert clean(payload) == {"b": 0, "c": False, "g": [{"i": 1}]}
    assert dump({"a": 1, "b": None}) == '{"a":1}'
