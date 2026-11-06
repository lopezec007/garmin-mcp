"""The approval page: where the owner types the password to authorize a client.

``/authorize`` (handled by the MCP SDK) redirects the browser here with a
signed transaction id. Approving needs the owner password every time; there
is no login session or cookie, so there is nothing to steal or forge.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import deque
from html import escape
from urllib.parse import parse_qs

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .oauth import (
    GarminOAuthProvider,
    PendingAuthorization,
    describe_redirect,
    normalize_url,
    printable,
    verify_password,
)

logger = logging.getLogger(__name__)

# The form carries a transaction id (a few kB at most) and a password.
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 8

_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # No form-action directive on purpose: browsers apply it to the redirect
    # that follows the form post, which goes to the client's redirect URI.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} - Garmin MCP</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 2rem 1rem;
         background: Canvas; color: CanvasText; }}
  main {{ max-width: 28rem; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 1rem; }}
  p {{ margin: 0 0 1rem; }}
  .warn {{ border: 2px solid #b26a00; border-radius: .5rem; padding: .75rem 1rem;
           margin: 0 0 1rem; font-weight: 600; }}
  .who {{ border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
          border-radius: .5rem; padding: .75rem 1rem; margin: 0 0 1rem;
          overflow-wrap: anywhere; }}
  .who small {{ display: block; opacity: .7; margin: 0 0 .5rem; }}
  .dest {{ display: block; font-weight: 700; font-size: 1.1rem; }}
  .error {{ color: #c62828; font-weight: 600; }}
  label {{ display: block; font-weight: 600; margin: 0 0 .25rem; }}
  input[type=password] {{ width: 100%; box-sizing: border-box; font: inherit;
          padding: .5rem; margin: 0 0 1rem; }}
  .actions {{ display: flex; gap: .5rem; }}
  button {{ font: inherit; padding: .5rem 1rem; cursor: pointer; }}
  code {{ font-size: .9em; }}
</style>
</head>
<body><main>
<h1>{title}</h1>
{body}
</main></body>
</html>
"""

_FORM = """
<p class="warn">Approve only a request you started yourself, a moment ago, from an app you
trust. If a link to this page was sent to you, it is an attack: close it.</p>
<p>An application is asking for read-only access to this Garmin Connect account.
The request was started {age}.</p>
<div class="who">
  It calls itself <strong>{client_name}</strong>
  <small>That name is chosen by the application and proves nothing.</small>
  {destination}
</div>
{error}
<form method="post" action="{action}">
  <input type="hidden" name="txn" value="{txn}">
  <label for="password">Owner password</label>
  <input type="password" id="password" name="password" autocomplete="current-password"
         autofocus>
  <div class="actions">
    <button type="submit" name="action" value="approve">Approve</button>
    <button type="submit" name="action" value="deny" formnovalidate>Deny</button>
  </div>
</form>
"""

_DEST_WEB = (
    "After approval your browser is sent, with the access grant, to"
    '<span class="dest">{target}</span>'
    "Check that this is the site of the app you are connecting."
)
_DEST_APP = (
    "After approval your browser opens this app link, with the access grant:"
    '<span class="dest">{target}</span>'
    "It is not a web address, so it is handled by whichever app on this device "
    "claims it. Approve only if it belongs to the app you are connecting."
)


def _page(title: str, body: str, status_code: int = 200, **headers: str) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(title=escape(title), body=body),
        status_code=status_code,
        headers={**_HEADERS, **headers},
    )


def _message(title: str, text: str, status_code: int, **headers: str) -> HTMLResponse:
    return _page(title, f"<p>{text}</p>", status_code, **headers)


def _age(started_at: float) -> str:
    seconds = max(0, int(time.time() - started_at))
    if seconds < 60:
        return "less than a minute ago"
    return f"{seconds // 60} minute(s) ago"


def _form(
    path: str, txn: str, pending: PendingAuthorization, error: str = "", status_code: int = 200
) -> HTMLResponse:
    target, is_web = describe_redirect(str(pending.params.redirect_uri))
    body = _FORM.format(
        age=_age(pending.started_at),
        client_name=escape(printable(pending.client_name) or "(no name)"),
        destination=(_DEST_WEB if is_web else _DEST_APP).format(target=escape(target)),
        error=f'<p class="error">{escape(error)}</p>' if error else "",
        action=escape(path),
        txn=escape(txn),
    )
    return _page("Authorize access", body, status_code)


def rate_limit_key(address: str) -> str:
    """One bucket per IPv4 address, and per /64 for IPv6 (a single subscriber
    controls a whole /64, so per-address buckets there limit nothing)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


class AttemptLimiter:
    """Bounds password guessing: per client address, and overall."""

    def __init__(
        self,
        per_address: int = 5,
        overall: int = 100,
        window: float = 900.0,
        max_addresses: int = 10_000,
    ) -> None:
        self._per_address = per_address
        self._overall = overall
        self._window = window
        self._max_addresses = max_addresses
        self._by_address: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _trim(self, attempts: deque[float], now: float) -> None:
        while attempts and attempts[0] <= now - self._window:
            attempts.popleft()

    def retry_after(self, address: str) -> int:
        """Seconds until ``address`` may try again; 0 if it may try now."""
        key = rate_limit_key(address)
        now = time.monotonic()
        self._trim(self._all, now)
        wait = 0.0
        if len(self._all) >= self._overall:
            wait = self._all[0] + self._window - now
        attempts = self._by_address.get(key)
        if attempts is not None:
            self._trim(attempts, now)
            if not attempts:
                del self._by_address[key]
            elif len(attempts) >= self._per_address:
                wait = max(wait, attempts[0] + self._window - now)
        return int(wait) + 1 if wait > 0 else 0

    def record_failure(self, address: str) -> None:
        key = rate_limit_key(address)
        now = time.monotonic()
        self._all.append(now)
        if key not in self._by_address and len(self._by_address) >= self._max_addresses:
            # Full: forget addresses whose failures have all aged out.
            for stale in [k for k, v in self._by_address.items() if v[-1] <= now - self._window]:
                del self._by_address[stale]
            if len(self._by_address) >= self._max_addresses:
                return  # still full; the overall limit keeps holding
        self._by_address.setdefault(key, deque()).append(now)

    def record_success(self, address: str) -> None:
        self._by_address.pop(rate_limit_key(address), None)


async def _read_form(request: Request) -> dict[str, str] | None:
    """The posted form, or None if it is not a small urlencoded form.

    Parsed by hand instead of with ``request.form()``: that accepts multipart
    uploads of any size, and this endpoint is reachable by anyone.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FORM_BYTES:
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=MAX_FORM_FIELDS,
        )
    except (ValueError, UnicodeDecodeError):
        return None
    return {name: values[0] for name, values in fields.items()}


def _is_cross_site(request: Request, issuer: str) -> bool:
    """True if a browser says this POST did not come from our own page."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False  # non-browser client, or an old browser: nothing to go on
    try:
        return normalize_url(origin) != normalize_url(issuer)
    except ValueError:
        return True  # "null" or garbage


def make_login_endpoint(provider: GarminOAuthProvider, path: str):
    limiter = AttemptLimiter()
    # scrypt takes ~100 ms and 32 MiB per check; never run many at once.
    verify_slots = anyio.CapacityLimiter(2)
    warned_no_forwarded_for = False

    expired = (
        "This sign-in request has expired or was already used. "
        "Go back to the application and start connecting again."
    )

    def client_address(request: Request) -> str:
        nonlocal warned_no_forwarded_for
        address = request.client.host if request.client else "unknown"
        if (
            not warned_no_forwarded_for
            and "x-forwarded-for" not in request.headers
            and provider.issuer.startswith("https://")
        ):
            # The public URL is https, so there is a proxy in front; if it does
            # not say who the client is, every visitor shares one bucket.
            warned_no_forwarded_for = True
            logger.warning(
                "Login request from %s carries no X-Forwarded-For header: the reverse proxy "
                "is not passing client addresses, so the per-address login limit cannot tell "
                "visitors apart.",
                address,
            )
        return address

    async def login(request: Request) -> Response:
        if request.method != "POST":
            txn = request.query_params.get("txn", "")
            pending = provider.pending(txn)
            if pending is None:
                return _message("Request expired", expired, 400)
            return _form(path, txn, pending)

        if _is_cross_site(request, provider.issuer):
            return _message(
                "Request refused", "This form must be submitted from its own page.", 403
            )
        form = await _read_form(request)
        if form is None:
            return _message("Bad request", "The submitted form could not be read.", 400)
        txn = form.get("txn", "")
        password = form.get("password", "")
        pending = provider.pending(txn)
        if pending is None:
            return _message("Request expired", expired, 400)

        if form.get("action") == "deny":
            target = provider.deny(txn)
            if target is None:
                return _message("Request expired", expired, 400)
            return RedirectResponse(target, status_code=303, headers=_HEADERS)

        password_hash = provider.store.password_hash()
        if password_hash is None:
            return _message(
                "Not set up yet",
                "No owner password has been set on the server. Run "
                "<code>sudo garmin-mcp set-password</code> there, then try again.",
                503,
            )

        address = client_address(request)
        # Checked while holding a slot, so a burst of parallel guesses cannot
        # all slip past the limit before the first failure is recorded.
        async with verify_slots:
            wait = limiter.retry_after(address)
            if wait:
                logger.warning("Login attempt from %s refused: too many failures", address)
                minutes = max(1, round(wait / 60))
                return _message(
                    "Too many attempts",
                    f"Too many failed attempts. Try again in about {minutes} minute(s).",
                    429,
                    **{"Retry-After": str(wait)},
                )
            ok = bool(password) and await anyio.to_thread.run_sync(
                verify_password, password, password_hash
            )
            if not ok:
                limiter.record_failure(address)
        if not ok:
            logger.warning("Failed login attempt from %s for client %s", address, pending.client_id)
            return _form(path, txn, pending, "Wrong password.", 401)

        limiter.record_success(address)
        target = provider.approve(txn)
        if target is None:  # expired while the password was being checked
            return _message("Request expired", expired, 400)
        return RedirectResponse(target, status_code=303, headers=_HEADERS)

    return login
