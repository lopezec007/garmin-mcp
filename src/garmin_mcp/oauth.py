"""Built-in OAuth 2.1 authorization server, for exposing the endpoint publicly.

The MCP SDK supplies the protocol endpoints (metadata discovery, dynamic client
registration, ``/authorize``, ``/token``, ``/revoke``, PKCE and bearer checks).
This module supplies what the SDK leaves to the application:

* :class:`OAuthStore` -- clients, authorization codes and tokens in SQLite, so
  authorized clients survive restarts. Tokens and codes are stored as SHA-256
  hashes; the owner password as an scrypt hash.
* :class:`GarminOAuthProvider` -- issues codes and tokens, rotates refresh
  tokens (revoking the grant when a rotated one is replayed), and binds every
  token to this server's resource URL.
* The rules for what a self-registered client may ask for. Registration is
  open to the internet, so everything a client supplies is size-capped and
  redirect URIs are restricted to targets a browser should be sent to.

There is exactly one resource owner: whoever knows the owner password. The
approval page that asks for it lives in :mod:`garmin_mcp.login`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

logger = logging.getLogger(__name__)

OWNER = "owner"
STATIC_CLIENT_ID = "static-token"
LOGIN_PATH = "/oauth/login"

CODE_TTL = 60  # seconds an authorization code stays exchangeable
PENDING_TTL = 300  # seconds the approval page stays valid
# After a refresh token is rotated, the old one keeps working this long, so a
# client that refreshes from two places at once (or retries after a lost
# response) is not thrown back to the login page -- but only a few times.
REFRESH_GRACE = 60
REFRESH_GRACE_USES = 3

# Registration is open to the internet, so bound what it can make us keep.
MAX_UNAUTHORIZED_CLIENTS = 2000
UNAUTHORIZED_CLIENT_TTL = 24 * 3600
# A registration this young is never evicted to make room: its owner may be
# looking at the approval page right now.
UNAUTHORIZED_CLIENT_MIN_AGE = 15 * 60
MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LENGTH = 2000
MAX_CLIENT_NAME_LENGTH = 200
MAX_CLIENT_RECORD_BYTES = 16 * 1024
MAX_STATE_LENGTH = 2048
MIN_PASSWORD_LENGTH = 12

# Schemes a browser must never be redirected to with an authorization code:
# script and local-file schemes, schemes that reach another network host, and
# ones that launch operating-system handlers. App-specific schemes
# (cursor://, vscode://, ...) are what native clients legitimately use.
_FORBIDDEN_REDIRECT_SCHEMES = {
    "javascript", "data", "vbscript", "file", "blob", "about", "filesystem", "view-source",
    "jar", "resource", "res", "chrome", "chrome-extension", "edge", "moz-extension",
    "ftp", "ftps", "sftp", "tftp", "ws", "wss", "smb", "nfs", "afp", "webcal", "webdav",
    "dav", "gopher", "ssh", "telnet", "ldap", "ldaps", "rtsp", "irc", "ircs", "magnet",
    "mailto", "tel", "sms", "facetime", "callto", "intent", "market", "search-ms",
}  # fmt: skip
_FORBIDDEN_REDIRECT_SCHEME_PREFIXES = ("ms-", "x-apple", "itms")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*$")
_CODE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
# Control characters and the Unicode bidi/format characters used to disguise text.
_UNPRINTABLE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")


def printable(text: str | None, limit: int = 80) -> str:
    """Client-supplied text made safe to show to the owner (page, terminal, log)."""
    cleaned = _UNPRINTABLE_RE.sub("", text or "").strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


# --- owner password -----------------------------------------------------------

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 2**26


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# --- storage --------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    authorized_at INTEGER
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    grant_id TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    client_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    rotated_at INTEGER,
    rotations INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens (grant_id);
CREATE INDEX IF NOT EXISTS tokens_client ON tokens (client_id);
"""


class StoreFull(Exception):
    """Too many recent unapproved registrations to accept another."""


@dataclass(frozen=True)
class StoredToken:
    kind: str
    client_id: str
    grant_id: str
    scopes: list[str]
    resource: str | None
    expires_at: int
    rotated_at: int | None
    rotations: int


class OAuthStore:
    """SQLite-backed state. Safe to share between the service and the admin CLI."""

    def __init__(self, path: Path, *, create: bool = True) -> None:
        self.path = path
        if not path.exists():
            if not create:
                raise FileNotFoundError(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create it private before SQLite writes anything into it.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.Lock()
        # Store calls run on the event loop; a short busy timeout keeps a
        # stuck lock (another process mid-write) from freezing the server.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=2)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # -- owner password --

    def password_hash(self) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = 'owner_password'")
        return row["value"] if row else None

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
        self._run(
            "INSERT INTO meta (key, value) VALUES ('owner_password', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (hash_password(password),),
        )

    # -- clients --

    def get_client_json(self, client_id: str) -> str | None:
        row = self._one("SELECT data FROM clients WHERE client_id = ?", (client_id,))
        return row["data"] if row else None

    def add_client(self, client_id: str, data: str) -> None:
        """Store a new, not yet approved registration. Raises :class:`StoreFull`."""
        now = int(time.time())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # Make room by dropping registrations nobody approved: expired
                # ones, then the oldest -- but never one young enough that its
                # owner could still be on the approval page.
                self._db.execute(
                    "DELETE FROM clients WHERE authorized_at IS NULL AND created_at < ?",
                    (now - UNAUTHORIZED_CLIENT_TTL,),
                )
                self._db.execute(
                    "DELETE FROM clients WHERE authorized_at IS NULL AND created_at < ? "
                    "AND client_id NOT IN ("
                    "  SELECT client_id FROM clients WHERE authorized_at IS NULL "
                    "  ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                    (now - UNAUTHORIZED_CLIENT_MIN_AGE, MAX_UNAUTHORIZED_CLIENTS - 1),
                )
                count = self._db.execute(
                    "SELECT COUNT(*) FROM clients WHERE authorized_at IS NULL"
                ).fetchone()[0]
                if count >= MAX_UNAUTHORIZED_CLIENTS:
                    raise StoreFull
                self._db.execute(
                    "INSERT INTO clients (client_id, data, created_at) VALUES (?, ?, ?)",
                    (client_id, data, now),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def mark_client_authorized(self, client_id: str) -> None:
        self._run(
            "UPDATE clients SET authorized_at = ? WHERE client_id = ?",
            (int(time.time()), client_id),
        )

    def list_clients(self) -> list[dict[str, Any]]:
        """Clients the owner has approved, with their live-token state (for the CLI)."""
        now = int(time.time())
        rows = self._all(
            "SELECT c.client_id, c.data, c.created_at, c.authorized_at, "
            "  (SELECT MAX(t.created_at) FROM tokens t WHERE t.client_id = c.client_id) "
            "    AS last_token_at, "
            "  (SELECT MAX(t.expires_at) FROM tokens t WHERE t.client_id = c.client_id "
            "     AND t.expires_at > ? AND t.rotated_at IS NULL) AS valid_until "
            "FROM clients c WHERE c.authorized_at IS NOT NULL ORDER BY c.authorized_at DESC",
            (now,),
        )
        out = []
        for row in rows:
            data = json.loads(row["data"])
            out.append(
                {
                    "client_id": row["client_id"],
                    "name": data.get("client_name"),
                    "redirect_uris": data.get("redirect_uris") or [],
                    "authorized_at": row["authorized_at"],
                    "last_token_at": row["last_token_at"],
                    "valid_until": row["valid_until"],
                }
            )
        return out

    # -- authorization codes --

    def add_code(self, code: AuthorizationCode) -> None:
        self._run(
            "INSERT INTO auth_codes (code_hash, data, expires_at) VALUES (?, ?, ?)",
            (_hash_token(code.code), code.model_dump_json(exclude={"code"}), int(code.expires_at)),
        )

    def get_code(self, code: str) -> tuple[AuthorizationCode, str | None] | None:
        """The stored code and, if it was already exchanged, the grant it produced."""
        row = self._one(
            "SELECT data, grant_id FROM auth_codes WHERE code_hash = ?", (_hash_token(code),)
        )
        if row is None:
            return None
        loaded = AuthorizationCode.model_validate({**json.loads(row["data"]), "code": code})
        return loaded, row["grant_id"]

    def use_code(self, code: str, grant_id: str) -> bool:
        """Atomically mark a code as exchanged. False if someone got there first."""
        cursor = self._run(
            "UPDATE auth_codes SET grant_id = ? WHERE code_hash = ? AND grant_id IS NULL",
            (grant_id, _hash_token(code)),
        )
        return cursor.rowcount == 1

    # -- tokens --

    def add_token(
        self,
        kind: str,
        token: str,
        *,
        client_id: str,
        grant_id: str,
        scopes: list[str],
        resource: str | None,
        expires_at: int,
    ) -> None:
        self._run(
            "INSERT INTO tokens (token_hash, kind, client_id, grant_id, scopes, resource, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash_token(token),
                kind,
                client_id,
                grant_id,
                json.dumps(scopes),
                resource,
                expires_at,
                int(time.time()),
            ),
        )

    def get_token(self, kind: str, token: str) -> StoredToken | None:
        """An unexpired token of this kind. Rotated refresh tokens are returned
        too (with ``rotated_at`` set); the caller decides what that means."""
        row = self._one(
            "SELECT kind, client_id, grant_id, scopes, resource, expires_at, rotated_at, rotations "
            "FROM tokens WHERE token_hash = ? AND kind = ?",
            (_hash_token(token), kind),
        )
        if row is None or row["expires_at"] < time.time():
            return None
        return StoredToken(
            kind=row["kind"],
            client_id=row["client_id"],
            grant_id=row["grant_id"],
            scopes=json.loads(row["scopes"]),
            resource=row["resource"],
            expires_at=row["expires_at"],
            rotated_at=row["rotated_at"],
            rotations=row["rotations"],
        )

    def rotate_token(self, token: str) -> bool:
        """Record one more use of a refresh token. False once the grace uses are spent."""
        cursor = self._run(
            "UPDATE tokens SET rotated_at = COALESCE(rotated_at, ?), rotations = rotations + 1 "
            "WHERE token_hash = ? AND kind = 'refresh' AND rotations < ?",
            (int(time.time()), _hash_token(token), REFRESH_GRACE_USES),
        )
        return cursor.rowcount == 1

    def expire_token(self, token: str, expires_at: int) -> None:
        self._run(
            "UPDATE tokens SET expires_at = MIN(expires_at, ?) WHERE token_hash = ?",
            (expires_at, _hash_token(token)),
        )

    def revoke_grant(self, grant_id: str) -> int:
        return self._run("DELETE FROM tokens WHERE grant_id = ?", (grant_id,)).rowcount

    def revoke_other_grants(self, client_id: str, keep_grant_id: str) -> int:
        return self._run(
            "DELETE FROM tokens WHERE client_id = ? AND grant_id != ?", (client_id, keep_grant_id)
        ).rowcount

    def revoke_client(self, client_id: str) -> bool:
        """Forget a client and everything issued to it. False if it does not exist."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM tokens WHERE client_id = ?", (client_id,))
                deleted = self._db.execute(
                    "DELETE FROM clients WHERE client_id = ?", (client_id,)
                ).rowcount
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return deleted == 1

    def revoke_all(self) -> int:
        """Forget every client, code and token. Returns the number of clients removed."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM tokens")
                self._db.execute("DELETE FROM auth_codes")
                count = self._db.execute(
                    "SELECT COUNT(*) FROM clients WHERE authorized_at IS NOT NULL"
                ).fetchone()[0]
                self._db.execute("DELETE FROM clients")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return count

    def prune(self) -> None:
        now = int(time.time())
        # Rotated refresh tokens stay until their original expiry, so that a
        # replay of one is recognised for what it is.
        self._run("DELETE FROM tokens WHERE expires_at < ?", (now,))
        # Likewise exchanged codes are kept a while past expiry.
        self._run("DELETE FROM auth_codes WHERE expires_at < ?", (now - 3600,))


# --- what clients may register ---------------------------------------------------


def normalize_url(url: str) -> str:
    """Scheme and host lower-cased, default port and trailing slash dropped.

    Raises ValueError for something that is not a usable URL.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        raise ValueError(f"not an absolute URL: {url!r}")
    if ":" in host:
        host = f"[{host}]"
    default_port = {"http": 80, "https": 443}.get(scheme)
    port = f":{parts.port}" if parts.port and parts.port != default_port else ""
    return f"{scheme}://{host}{port}{parts.path.rstrip('/')}"


def _is_loopback(host: str | None) -> bool:
    host = (host or "").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_redirect_uri(uri: str) -> None:
    """Refuse redirect targets that have no business receiving an authorization code."""

    def refuse(reason: str) -> RegistrationError:
        return RegistrationError("invalid_redirect_uri", f"redirect URI not allowed: {reason}")

    if len(uri) > MAX_REDIRECT_URI_LENGTH:
        raise refuse(f"longer than {MAX_REDIRECT_URI_LENGTH} characters")
    if _UNPRINTABLE_RE.search(uri) or " " in uri:
        raise refuse("contains whitespace or control characters")
    try:
        parts = urlsplit(uri)
        port = parts.port  # noqa: F841  (accessing it validates it)
    except ValueError:
        raise refuse("not a valid URL") from None
    scheme = parts.scheme.lower()
    if not _SCHEME_RE.match(scheme):
        raise refuse("no valid scheme")
    if scheme in _FORBIDDEN_REDIRECT_SCHEMES or scheme.startswith(
        _FORBIDDEN_REDIRECT_SCHEME_PREFIXES
    ):
        raise refuse(f"scheme {scheme}: is not accepted")
    if parts.fragment:
        raise refuse("has a fragment")
    # "https://claude.ai@evil.example/" goes to evil.example; never accept it.
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise refuse("contains user information (an @ before the host)")
    if scheme in ("http", "https"):
        if not parts.hostname:
            raise refuse("has no host")
        if scheme == "http" and not _is_loopback(parts.hostname):
            raise refuse("plain http is only accepted for localhost; use https")


def describe_redirect(uri: str) -> tuple[str, bool]:
    """How to show a redirect target to the owner: ``(text, is_web_address)``.

    For web addresses that is the host (never the user-info part, never the
    path); for anything else the whole URI, because there is no trustworthy
    short form of an app-specific address.
    """
    parts = urlsplit(uri)
    if parts.scheme.lower() in ("http", "https") and parts.hostname:
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        return (f"{host}:{port}" if port else host), True
    return printable(uri, 200), False


class RegisteredClient(OAuthClientInformationFull):
    """A stored client. Loopback redirect URIs match on any port (RFC 8252 7.3):
    native apps listen on an ephemeral port that changes between sign-ins."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        try:
            return super().validate_redirect_uri(redirect_uri)
        except InvalidRedirectUriError:
            if redirect_uri is None:
                raise
            requested = urlsplit(str(redirect_uri))
            if requested.scheme != "http" or not _is_loopback(requested.hostname):
                raise
            if requested.username is not None or "@" in requested.netloc:
                raise
            for registered in self.redirect_uris or []:
                known = urlsplit(str(registered))
                if (
                    known.scheme == "http"
                    and _is_loopback(known.hostname)
                    and known.hostname == requested.hostname
                    and (known.path or "/") == (requested.path or "/")
                    and known.query == requested.query
                ):
                    return redirect_uri
            raise


# --- provider -------------------------------------------------------------------


@dataclass(frozen=True)
class PendingAuthorization:
    """An /authorize request waiting for the owner to approve it on the login page."""

    client_id: str
    client_name: str | None
    params: AuthorizationParams
    started_at: float
    expires_at: float


class GarminOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self,
        store: OAuthStore,
        *,
        issuer: str | None = None,
        mcp_path: str = "/mcp",
        static_token: str | None = None,
        access_ttl: int = 3600,
        refresh_ttl: int = 30 * 24 * 3600,
    ) -> None:
        self.store = store
        # ``issuer`` is the configured public origin (GARMIN_MCP_PUBLIC_URL).
        # Without one the server answers to whatever address a client used to
        # reach it, and ``issuer``/``resource_url`` are None.
        self.issuer = issuer
        self.mcp_path = mcp_path
        self.resource_url = f"{issuer}{mcp_path}" if issuer else None
        self._accepted_resources = (
            {normalize_url(self.resource_url), normalize_url(issuer)} if issuer else None
        )
        self._static_token = static_token.encode() if static_token else None
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        # Pending approvals are not stored: the transaction id handed to the
        # browser is the authorization request itself, signed with this key.
        # Nothing an anonymous visitor does can fill or evict anything. The key
        # is per process, so approval pages open across a restart just expire.
        self._txn_key = secrets.token_bytes(32)
        # Transactions that already produced a code (password-gated, so small).
        self._approved: dict[str, float] = {}

    # -- clients --

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client_json(client_id)
        return RegisteredClient.model_validate_json(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = client_info.redirect_uris or []
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError(
                "invalid_redirect_uri", f"register between 1 and {MAX_REDIRECT_URIS} redirect URIs"
            )
        for uri in uris:
            check_redirect_uri(str(uri))
        if len(client_info.client_name or "") > MAX_CLIENT_NAME_LENGTH:
            raise RegistrationError(
                "invalid_client_metadata",
                f"client_name is longer than {MAX_CLIENT_NAME_LENGTH} characters",
            )
        data = client_info.model_dump_json(exclude_none=True)
        if len(data.encode()) > MAX_CLIENT_RECORD_BYTES:
            raise RegistrationError(
                "invalid_client_metadata",
                f"client metadata is larger than {MAX_CLIENT_RECORD_BYTES} bytes",
            )
        try:
            self.store.add_client(client_info.client_id, data)
        except StoreFull:
            logger.warning("Refusing client registration: too many unapproved registrations")
            raise RegistrationError(
                "invalid_client_metadata",
                "too many pending registrations; try again in a few minutes",
            ) from None
        logger.info(
            "Registered OAuth client %s (%r)",
            client_info.client_id,
            printable(client_info.client_name),
        )

    # -- authorization: /authorize -> login page -> code --

    def _sign(self, payload: bytes) -> str:
        return _b64url(hmac.new(self._txn_key, payload, hashlib.sha256).digest())

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource:
            try:
                wanted = normalize_url(params.resource)
            except ValueError:
                wanted = None
            if self._accepted_resources is not None:
                acceptable = wanted in self._accepted_resources
            else:
                # No fixed address: the client names us by the address it used,
                # which we cannot know in advance. Its path must still be ours.
                acceptable = wanted is not None and urlsplit(wanted).path in (
                    "",
                    self.mcp_path.rstrip("/"),
                )
            if not acceptable:
                raise AuthorizeError(
                    "invalid_target",
                    "this server only issues tokens for its own MCP endpoint "
                    f"({self.resource_url or self.mcp_path})",
                )
        if not _CODE_CHALLENGE_RE.match(params.code_challenge):
            raise AuthorizeError("invalid_request", "code_challenge is not a valid S256 challenge")
        if params.state is not None and len(params.state) > MAX_STATE_LENGTH:
            raise AuthorizeError(
                "invalid_request", f"state is longer than {MAX_STATE_LENGTH} characters"
            )
        now = int(time.time())
        payload = _b64url(
            json.dumps(
                {
                    "c": client.client_id,
                    "p": params.model_dump(mode="json"),
                    "t": now,
                    "n": secrets.token_urlsafe(9),
                },
                separators=(",", ":"),
            ).encode()
        )
        txn = f"{payload}.{self._sign(payload.encode())}"
        # Relative when there is no fixed address: the browser resolves it
        # against the address it used for /authorize.
        return construct_redirect_uri((self.issuer or "") + LOGIN_PATH, txn=txn)

    def pending(self, txn: str) -> PendingAuthorization | None:
        """Decode a transaction id; None if forged, expired, used, or its client is gone."""
        payload, _, signature = txn.partition(".")
        if not payload or not hmac.compare_digest(signature, self._sign(payload.encode())):
            return None
        if txn in self._approved:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            started_at = float(data["t"])
            params = AuthorizationParams.model_validate(data["p"])
            client_id = str(data["c"])
        except (ValueError, KeyError, TypeError):
            return None
        if started_at + PENDING_TTL < time.time():
            return None
        client_json = self.store.get_client_json(client_id)
        if client_json is None:
            return None
        return PendingAuthorization(
            client_id=client_id,
            client_name=json.loads(client_json).get("client_name"),
            params=params,
            started_at=started_at,
            expires_at=started_at + PENDING_TTL,
        )

    def approve(self, txn: str) -> str | None:
        """The owner approved: mint a code and return where to send the browser."""
        pending = self.pending(txn)
        if pending is None:
            return None
        now = time.time()
        self._approved = {t: exp for t, exp in self._approved.items() if exp > now}
        self._approved[txn] = pending.expires_at
        params = pending.params
        code = secrets.token_urlsafe(32)
        self.store.add_code(
            AuthorizationCode(
                code=code,
                scopes=params.scopes or [],
                expires_at=now + CODE_TTL,
                client_id=pending.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                # Always this server: tokens are never issued for anything else.
                resource=self.resource_url,
                subject=OWNER,
            )
        )
        logger.info(
            "Owner approved OAuth client %s (%r), redirecting to %s",
            pending.client_id,
            printable(pending.client_name),
            describe_redirect(str(params.redirect_uri))[0],
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, txn: str) -> str | None:
        pending = self.pending(txn)
        if pending is None:
            return None
        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            error="access_denied",
            error_description="The owner declined the request",
            state=pending.params.state,
        )

    # -- token endpoint --

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        found = self.store.get_code(authorization_code)
        if found is None:
            return None
        code, grant_id = found
        if grant_id is not None:
            # A code presented twice means it leaked: kill what it produced.
            logger.warning("Authorization code replayed for client %s; revoking", code.client_id)
            self.store.revoke_grant(grant_id)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        grant_id = secrets.token_urlsafe(16)
        if not self.store.use_code(authorization_code.code, grant_id):
            raise TokenError("invalid_grant", "authorization code has already been used")
        client_id = authorization_code.client_id
        # A fresh approval replaces whatever this client held before, so an
        # old (possibly stolen) session does not live on beside the new one.
        self.store.revoke_other_grants(client_id, grant_id)
        self.store.mark_client_authorized(client_id)
        return self._issue(client_id, grant_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = self.store.get_token("refresh", refresh_token)
        if stored is None:
            return None
        if stored.rotated_at is not None and time.time() > stored.rotated_at + REFRESH_GRACE:
            # A refresh token that was already replaced is being used again:
            # either it was stolen or the thief is the one who replaced it.
            # Either way the whole grant goes, and the client signs in again.
            logger.warning(
                "Rotated refresh token replayed for client %s; revoking its grant",
                stored.client_id,
            )
            self.store.revoke_grant(stored.grant_id)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            resource=stored.resource,
            subject=OWNER,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        stored = self.store.get_token("refresh", refresh_token.token)
        if stored is None or not self.store.rotate_token(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        return self._issue(stored.client_id, stored.grant_id, scopes)

    def _issue(self, client_id: str, grant_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        common: dict[str, Any] = {
            "client_id": client_id,
            "grant_id": grant_id,
            "scopes": scopes,
            "resource": self.resource_url,
        }
        self.store.add_token("access", access, expires_at=now + self._access_ttl, **common)
        self.store.add_token("refresh", refresh, expires_at=now + self._refresh_ttl, **common)
        self.store.prune()
        return OAuthToken(
            access_token=access,
            expires_in=self._access_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    # -- resource server --

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._static_token and hmac.compare_digest(token.encode(), self._static_token):
            # GARMIN_MCP_AUTH_TOKEN keeps working next to OAuth (LAN clients,
            # scripts). It never expires; remove it from the config to disable.
            return AccessToken(
                token=token,
                client_id=STATIC_CLIENT_ID,
                scopes=[],
                resource=self.resource_url,
                subject=OWNER,
            )
        stored = self.store.get_token("access", token)
        if stored is None:
            return None
        return AccessToken(
            token=token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            # Tokens are opaque and only exist in this server's own database,
            # so any token found there was issued for this server. Report the
            # current address rather than the one recorded at issue time, so
            # tokens survive GARMIN_MCP_PUBLIC_URL being set or changed.
            resource=self.resource_url,
            subject=OWNER,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        stored = self.store.get_token(kind, token.token)
        if stored is not None:
            # Revoking either half of a grant revokes the whole grant.
            self.store.revoke_grant(stored.grant_id)
