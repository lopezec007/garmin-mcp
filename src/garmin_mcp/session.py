"""A single, lazily-authenticated Garmin Connect session shared by every tool.

``garminconnect`` is synchronous, so calls run in a worker thread and are
serialised with a lock: one account, one request at a time, which also keeps
us polite toward Garmin's rate limits.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Any

import anyio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings

logger = logging.getLogger(__name__)

LOGIN_HINT = (
    "Run `garmin-mcp login` on the server once (it prompts for the MFA code and "
    "saves tokens), or check GARMIN_EMAIL/GARMIN_PASSWORD in the service's env file."
)


class GarminLoginError(Exception):
    """Login to Garmin Connect failed or cannot be attempted right now."""


def _mfa_not_available() -> str:
    raise GarminLoginError(
        "Garmin asked for an MFA code, which the background service cannot provide. " + LOGIN_HINT
    )


class GarminSession:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._api: Garmin | None = None
        self._login_failed_at: float | None = None
        self._login_error: str | None = None

    # -- authentication (blocking; call under the lock) ----------------------

    def _new_client(self, prompt_mfa: Any = None) -> Garmin:
        return Garmin(
            email=self._settings.email,
            password=self._settings.password,
            is_cn=self._settings.is_cn,
            prompt_mfa=prompt_mfa or _mfa_not_available,
        )

    def login_sync(self, prompt_mfa: Any = None, *, force: bool = False) -> Garmin:
        """Return a logged-in client, logging in (tokens first, then credentials) if needed.

        A failed login is remembered for ``login_retry_cooldown`` seconds and
        re-raised without contacting Garmin again, unless ``force`` is set.
        """
        with self._lock:
            if self._api is not None and not force:
                return self._api
            if not force and self._login_failed_at is not None:
                wait = self._settings.login_retry_cooldown - (
                    time.monotonic() - self._login_failed_at
                )
                if wait > 0:
                    raise GarminLoginError(
                        f"{self._login_error} (not retrying for another {int(wait)}s)"
                    )

            token_dir = self._settings.token_dir
            token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            api = self._new_client(prompt_mfa)
            try:
                api.login(str(token_dir))
            except GarminLoginError as exc:
                self._remember_failure(str(exc))
                raise
            except GarminConnectTooManyRequestsError as exc:
                self._remember_failure(f"Garmin is rate-limiting logins: {exc}")
                raise GarminLoginError(self._login_error) from exc
            except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
                self._remember_failure(f"Garmin login failed: {exc}. {LOGIN_HINT}")
                raise GarminLoginError(self._login_error) from exc
            self._api = api
            self._login_failed_at = None
            self._login_error = None
            logger.info("Logged in to Garmin Connect as %s", api.get_full_name() or "<unknown>")
            return api

    def _remember_failure(self, message: str) -> None:
        self._api = None
        self._login_failed_at = time.monotonic()
        self._login_error = message
        logger.error("%s", message)

    # -- API calls ------------------------------------------------------------

    def call_sync(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``Garmin.<method>(*args, **kwargs)``, mapping failures to ``ToolError``.

        If Garmin rejects the session mid-flight, log in again once and retry.
        """
        with self._lock:
            try:
                api = self.login_sync()
                try:
                    return getattr(api, method)(*args, **kwargs)
                except GarminConnectAuthenticationError:
                    logger.warning("Garmin session rejected during %s; logging in again", method)
                    api = self.login_sync(force=True)
                    return getattr(api, method)(*args, **kwargs)
            except GarminLoginError as exc:
                raise ToolError(str(exc)) from exc
            except GarminConnectNotFoundError as exc:
                raise ToolError(f"Garmin has no such resource ({method}): {exc}") from exc
            except GarminConnectTooManyRequestsError as exc:
                raise ToolError(
                    f"Garmin rate limit hit; wait a few minutes and retry. ({exc})"
                ) from exc
            except GarminConnectAuthenticationError as exc:
                raise ToolError(f"Garmin authentication failed: {exc}. {LOGIN_HINT}") from exc
            except GarminConnectConnectionError as exc:
                raise ToolError(f"Garmin Connect request failed ({method}): {exc}") from exc
            except (ValueError, TypeError) as exc:
                # garminconnect validates arguments (date formats, ranges, ids)
                # and raises these; the message is meant for the caller.
                raise ToolError(f"Invalid argument for {method}: {exc}") from exc
            except Exception as exc:
                # Typically garminconnect tripping over a response shape it did
                # not expect (KeyError, AttributeError). Keep the traceback in
                # the journal and give the caller something more useful than
                # the SDK's bare "Error executing tool".
                logger.exception("Unexpected error in Garmin call %s", method)
                raise ToolError(
                    f"Unexpected error from the Garmin library in {method}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Async wrapper around :meth:`call_sync` (runs in a worker thread)."""
        return await anyio.to_thread.run_sync(lambda: self.call_sync(method, *args, **kwargs))

    # -- dates ----------------------------------------------------------------

    def _today_sync(self) -> date:
        with self._lock:
            try:
                return self.login_sync().today()
            except GarminLoginError as exc:
                raise ToolError(str(exc)) from exc

    async def today(self) -> date:
        """Today's date in the Garmin account's timezone."""
        return await anyio.to_thread.run_sync(self._today_sync)

    async def date(self, value: str | None = None, *, default_days_ago: int = 0) -> str:
        """Resolve a user-supplied date to ``YYYY-MM-DD``.

        Accepts ``None`` (-> today minus ``default_days_ago``), ``"today"``,
        ``"yesterday"``, ``"tomorrow"``, ``"<N>d"``/``"-<N>d"`` (N days ago),
        ``"+<N>d"`` (N days ahead), or an ISO date.
        "Today" is evaluated in the Garmin account's timezone.
        """
        text = (value or "").strip().lower()
        if not text:
            return (await self.today() - timedelta(days=default_days_ago)).isoformat()
        if text == "today":
            return (await self.today()).isoformat()
        if text == "yesterday":
            return (await self.today() - timedelta(days=1)).isoformat()
        if text == "tomorrow":
            return (await self.today() + timedelta(days=1)).isoformat()
        if text.endswith("d") and text[:-1].lstrip("+-").isdigit() and text.count("+") <= 1:
            # "7d" / "-7d" = 7 days ago; "+7d" = 7 days ahead (for calendar tools).
            days = int(text[:-1].lstrip("+-"))
            delta = timedelta(days=days if text.startswith("+") else -days)
            return (await self.today() + delta).isoformat()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ToolError(
                f"Invalid date {value!r}: use YYYY-MM-DD, 'today', 'yesterday', 'tomorrow', "
                "'<N>d' for N days ago (e.g. '7d') or '+<N>d' for N days ahead."
            ) from None

    async def date_range(
        self, start: str | None, end: str | None, *, default_days: int = 7
    ) -> tuple[str, str]:
        """Resolve a ``(start, end)`` pair; defaults to the last ``default_days`` days."""
        end_s = await self.date(end)
        if start:
            start_s = await self.date(start)
        else:
            start_s = (date.fromisoformat(end_s) - timedelta(days=default_days - 1)).isoformat()
        if start_s > end_s:
            raise ToolError(f"start date {start_s} is after end date {end_s}")
        return start_s, end_s
