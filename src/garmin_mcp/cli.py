"""Command line entry point: ``garmin-mcp serve | login | check``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from dataclasses import replace

from . import __version__
from .config import Settings
from .session import GarminLoginError, GarminSession


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        # journald adds its own timestamps.
        format="%(levelname)s %(name)s: %(message)s",
    )


def cmd_serve(settings: Settings) -> int:
    import uvicorn

    from .server import build_app

    app = build_app(settings)
    logging.getLogger(__name__).info(
        "Serving MCP at http://%s:%s%s", settings.host, settings.port, settings.path
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=settings.log_level == "DEBUG",
    )
    return 0


def cmd_login(settings: Settings) -> int:
    """Interactive login: asks for whatever is missing, handles MFA, saves tokens."""
    email = settings.email or input("Garmin email: ").strip()
    password = settings.password or getpass.getpass("Garmin password: ")
    session = GarminSession(replace(settings, email=email, password=password))
    try:
        api = session.login_sync(prompt_mfa=lambda: input("Garmin MFA code: ").strip(), force=True)
    except GarminLoginError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Logged in as {api.get_full_name() or email}. Tokens saved in {settings.token_dir}")
    return 0


def cmd_check(settings: Settings) -> int:
    """Non-interactive: verify the saved tokens / credentials work."""
    try:
        api = GarminSession(settings).login_sync()
    except GarminLoginError as exc:
        print(f"Not logged in: {exc}", file=sys.stderr)
        return 1
    print(f"OK: logged in as {api.get_full_name() or '<unknown>'} (tokens: {settings.token_dir})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp", description="Garmin Connect MCP server (streamable HTTP)."
    )
    parser.add_argument("--version", action="version", version=f"garmin-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP HTTP server (default)")
    sub.add_parser("login", help="log in interactively (handles MFA) and save tokens")
    sub.add_parser("check", help="verify that saved tokens or credentials work")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    _configure_logging(settings.log_level)
    command = args.command or "serve"
    return {"serve": cmd_serve, "login": cmd_login, "check": cmd_check}[command](settings)


if __name__ == "__main__":
    raise SystemExit(main())
