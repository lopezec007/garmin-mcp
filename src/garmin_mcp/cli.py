"""Command line entry point: ``garmin-mcp <command>`` (see ``garmin-mcp --help``)."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import time
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
    if level != "DEBUG":
        # In stateless mode the SDK logs "Terminating session: None" at INFO
        # for every single request.
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)


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
        # Behind a reverse proxy, take the client address (used to rate-limit
        # password guesses) from X-Forwarded-For -- but only from that proxy.
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
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


def cmd_set_password(settings: Settings, args: argparse.Namespace) -> int:
    """Set the owner password that the OAuth approval page asks for."""
    from .oauth import MIN_PASSWORD_LENGTH, OAuthStore

    if sys.stdin.isatty():
        password = getpass.getpass(f"New owner password (at least {MIN_PASSWORD_LENGTH} chars): ")
        if password != getpass.getpass("Repeat it: "):
            print("The two entries differ; nothing changed.", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    store = OAuthStore(settings.oauth_db)
    try:
        store.set_password(password)
    except ValueError as exc:
        print(f"Not changed: {exc}.", file=sys.stderr)
        return 1
    print("Owner password set. It takes effect immediately; no restart needed.")
    print("Clients that are already authorized stay authorized (see `garmin-mcp revoke`).")
    if not settings.oauth_enabled:
        print("Note: OAuth is switched off in the configuration (GARMIN_MCP_OAUTH).")
    return 0


def _when(timestamp: int | None) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _existing_store(settings: Settings):
    """The OAuth database, without creating one where there is none.

    Run by hand outside the service's environment, these commands would
    otherwise happily report on (or "revoke everything" in) a brand-new empty
    database somewhere else.
    """
    from .oauth import OAuthStore

    try:
        return OAuthStore(settings.oauth_db, create=False)
    except FileNotFoundError:
        print(
            f"No OAuth database at {settings.oauth_db}. Nothing has been set up there; if the "
            "service is installed, run this through `sudo garmin-mcp` so it uses the "
            "service's database.",
            file=sys.stderr,
        )
        return None


def cmd_clients(settings: Settings, args: argparse.Namespace) -> int:
    """List the OAuth clients the owner has authorized."""
    from .oauth import printable

    store = _existing_store(settings)
    if store is None:
        return 1
    clients = store.list_clients()
    if not clients:
        print(f"No OAuth clients have been authorized ({settings.oauth_db}).")
        return 0
    for client in clients:
        state = (
            f"signed in until {_when(client['valid_until'])} (extended each time it is used)"
            if client["valid_until"]
            else "expired (must sign in again)"
        )
        # Names and URIs come from the clients themselves: never print them raw.
        print(f"{client['client_id']}  {printable(client['name']) or '(unnamed)'}")
        print(
            f"    authorized {_when(client['authorized_at'])}, "
            f"last token {_when(client['last_token_at'])}, {state}"
        )
        for uri in client["redirect_uris"]:
            print(f"    redirects to {printable(str(uri), 200)}")
    return 0


def cmd_revoke(settings: Settings, args: argparse.Namespace) -> int:
    """Sign out one OAuth client, or all of them."""
    store = _existing_store(settings)
    if store is None:
        return 1
    if args.all:
        print(f"Revoked every token and removed {store.revoke_all()} authorized client(s).")
        return 0
    if not args.client_id:
        print("Give a client id (see `garmin-mcp clients`) or --all.", file=sys.stderr)
        return 2
    if not store.revoke_client(args.client_id):
        print(f"No such client: {args.client_id}", file=sys.stderr)
        return 1
    print(f"Revoked client {args.client_id}.")
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
    sub.add_parser("set-password", help="set the owner password for the OAuth approval page")
    sub.add_parser("clients", help="list the OAuth clients that have been authorized")
    revoke = sub.add_parser("revoke", help="sign out one OAuth client, or all with --all")
    revoke.add_argument("client_id", nargs="?", help="client id from `garmin-mcp clients`")
    revoke.add_argument("--all", action="store_true", help="revoke every client and token")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"garmin-mcp: configuration error: {exc}", file=sys.stderr)
        return 2
    _configure_logging(settings.log_level)
    command = args.command or "serve"
    if command in ("serve", "login", "check"):
        return {"serve": cmd_serve, "login": cmd_login, "check": cmd_check}[command](settings)
    return {
        "set-password": cmd_set_password,
        "clients": cmd_clients,
        "revoke": cmd_revoke,
    }[command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
