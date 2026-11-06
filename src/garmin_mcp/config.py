"""Runtime configuration, read from environment variables.

Under systemd these come from ``/etc/garmin-mcp/garmin-mcp.env`` (see
``deploy/garmin-mcp.service``). Every variable is optional except that a first
login needs credentials: either ``GARMIN_EMAIL``/``GARMIN_PASSWORD`` here, or a
one-time interactive ``garmin-mcp login``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _default_token_dir() -> Path:
    # systemd sets STATE_DIRECTORY when the unit declares StateDirectory=.
    state_dir = os.environ.get("STATE_DIRECTORY")
    if state_dir:
        return Path(state_dir.split(":")[0]) / "tokens"
    return Path.home() / ".garminconnect"


@dataclass(frozen=True)
class Settings:
    email: str | None = None
    password: str | None = field(default=None, repr=False)
    token_dir: Path = field(default_factory=_default_token_dir)
    is_cn: bool = False

    host: str = "0.0.0.0"
    port: int = 8765
    path: str = "/mcp"
    # Optional shared secret. When set, clients must send
    # ``Authorization: Bearer <token>`` on every MCP request.
    auth_token: str | None = field(default=None, repr=False)
    # Optional Host-header allowlist (DNS-rebinding protection), e.g.
    # "garmin.lan:*,192.168.1.10:*". Empty means accept any Host.
    allowed_hosts: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    # Seconds to wait after a failed Garmin login before trying again, so a bad
    # password or a rate limit doesn't turn into a login storm against Garmin SSO.
    login_retry_cooldown: float = 300.0

    @classmethod
    def from_env(cls) -> Settings:
        token_dir = os.environ.get("GARMIN_TOKEN_DIR")
        path = os.environ.get("GARMIN_MCP_PATH", "/mcp").strip() or "/mcp"
        if not path.startswith("/"):
            path = "/" + path
        return cls(
            email=os.environ.get("GARMIN_EMAIL") or None,
            password=os.environ.get("GARMIN_PASSWORD") or None,
            token_dir=Path(token_dir).expanduser() if token_dir else _default_token_dir(),
            is_cn=_env_bool("GARMIN_IS_CN"),
            host=os.environ.get("GARMIN_MCP_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=int(os.environ.get("GARMIN_MCP_PORT", "8765")),
            path=path,
            auth_token=os.environ.get("GARMIN_MCP_AUTH_TOKEN") or None,
            allowed_hosts=_env_list("GARMIN_MCP_ALLOWED_HOSTS"),
            log_level=(os.environ.get("GARMIN_MCP_LOG_LEVEL", "INFO").strip() or "INFO").upper(),
            login_retry_cooldown=float(os.environ.get("GARMIN_MCP_LOGIN_COOLDOWN", "300")),
        )
