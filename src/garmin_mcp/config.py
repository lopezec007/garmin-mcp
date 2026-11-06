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
from urllib.parse import urlsplit

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _systemd_state_dir() -> Path | None:
    # systemd sets STATE_DIRECTORY when the unit declares StateDirectory=.
    state_dir = os.environ.get("STATE_DIRECTORY")
    return Path(state_dir.split(":")[0]) if state_dir else None


def _default_token_dir() -> Path:
    state_dir = _systemd_state_dir()
    return state_dir / "tokens" if state_dir else Path.home() / ".garminconnect"


def _default_oauth_db() -> Path:
    state_dir = _systemd_state_dir() or Path.home() / ".local" / "state" / "garmin-mcp"
    return state_dir / "oauth.db"


def _check_public_url(url: str) -> str:
    """Normalise GARMIN_MCP_PUBLIC_URL to ``scheme://host[:port]`` or raise ValueError."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    problem = None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        problem = "it must look like https://garmin.example.com"
    elif parts.path or parts.query or parts.fragment:
        problem = "it must be the bare origin, without a path (the server adds /mcp itself)"
    elif parts.username or parts.password:
        problem = "it must not contain credentials"
    elif parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        problem = "it must be https (plain http is accepted for localhost only, for testing)"
    if problem:
        raise ValueError(f"GARMIN_MCP_PUBLIC_URL={url!r} is not usable: {problem}")
    return url


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
    # The https origin clients reach this server at, e.g. https://garmin.example.com.
    # Setting it turns on the built-in OAuth authorization server.
    public_url: str | None = None
    oauth_db: Path = field(default_factory=_default_oauth_db)
    oauth_access_ttl: int = 3600
    oauth_refresh_ttl: int = 30 * 24 * 3600
    # Addresses whose X-Forwarded-For / X-Forwarded-Proto headers are believed
    # (the reverse proxy in front of the server). Comma-separated.
    trusted_proxies: str = "127.0.0.1"
    # Optional Host-header allowlist (DNS-rebinding protection), e.g.
    # "garmin.lan:*,192.168.1.10:*". Empty means accept any Host.
    allowed_hosts: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    # Seconds to wait after a failed Garmin login before trying again, so a bad
    # password or a rate limit doesn't turn into a login storm against Garmin SSO.
    login_retry_cooldown: float = 300.0

    def __post_init__(self) -> None:
        if self.public_url is not None:
            object.__setattr__(self, "public_url", _check_public_url(self.public_url))
            # Once the endpoint is on the internet, the static token is an
            # online-guessable secret with no rate limit: insist on a real one.
            if self.auth_token is not None and len(self.auth_token) < 32:
                raise ValueError(
                    "GARMIN_MCP_AUTH_TOKEN must be at least 32 characters when "
                    "GARMIN_MCP_PUBLIC_URL is set (generate one with `openssl rand -hex 32`), "
                    "or be removed to make OAuth the only way in"
                )
        if self.oauth_access_ttl < 60 or self.oauth_refresh_ttl < self.oauth_access_ttl:
            raise ValueError(
                "OAuth token lifetimes are not usable: the access token must live at least "
                "60 s and the refresh token at least as long as the access token"
            )

    @property
    def oauth_enabled(self) -> bool:
        return self.public_url is not None

    @property
    def resource_url(self) -> str | None:
        """The public URL of the MCP endpoint: what OAuth tokens are issued for."""
        return f"{self.public_url}{self.path}" if self.public_url else None

    @classmethod
    def from_env(cls) -> Settings:
        token_dir = os.environ.get("GARMIN_TOKEN_DIR")
        oauth_db = os.environ.get("GARMIN_MCP_OAUTH_DB")
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
            public_url=os.environ.get("GARMIN_MCP_PUBLIC_URL", "").strip() or None,
            oauth_db=Path(oauth_db).expanduser() if oauth_db else _default_oauth_db(),
            oauth_access_ttl=int(os.environ.get("GARMIN_MCP_OAUTH_ACCESS_TTL", "3600")),
            oauth_refresh_ttl=int(os.environ.get("GARMIN_MCP_OAUTH_REFRESH_TTL", "2592000")),
            trusted_proxies=os.environ.get("GARMIN_MCP_TRUSTED_PROXIES", "").strip() or "127.0.0.1",
            allowed_hosts=_env_list("GARMIN_MCP_ALLOWED_HOSTS"),
            log_level=(os.environ.get("GARMIN_MCP_LOG_LEVEL", "INFO").strip() or "INFO").upper(),
            login_retry_cooldown=float(os.environ.get("GARMIN_MCP_LOGIN_COOLDOWN", "300")),
        )
