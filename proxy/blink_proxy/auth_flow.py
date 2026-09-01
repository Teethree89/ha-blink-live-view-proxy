"""Managed Blink authentication state for the long-running proxy server."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger("blink-liveview-proxy")

# Matches PIN_WAIT_SECONDS in blink.py: the add-on's legacy option poller runs
# inside this same challenge, and a shorter budget here would cut its wait
# short and contradict the message it logs. Blink's own code expires first.
AUTH_TIMEOUT_SECONDS = 15 * 60
AUTH_STATES = {
    "idle",
    "authenticating",
    "waiting_for_pin",
    "success",
    "expired",
    "failure",
}

PUBLIC_MESSAGES = {
    "idle": "Ready to start Blink authentication.",
    "authenticating": "Contacting Blink securely…",
    "waiting_for_pin": "Blink sent a new PIN. Enter that PIN without restarting the proxy.",
    "success": "Blink authentication succeeded and the auth cache was saved.",
    "expired": "The Blink challenge expired. Start a new login to request a fresh PIN.",
    "credential_failure": "Blink rejected the login or could not start authentication.",
    "invalid_pin": "Blink rejected the PIN. Start a new login and use the newly issued PIN.",
    "failure": "Blink authentication failed. Start a new login and try again.",
}

class AuthFlowError(RuntimeError):
    """A classified authentication failure whose details are safe to redact."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

class AuthConflictError(RuntimeError):
    """Another login attempt already owns the OAuth challenge session."""

class StaleChallengeError(RuntimeError):
    """A PIN or cancellation referred to a challenge that is no longer active."""


ClientFactory = Callable[..., Any]
PinWaiter = Callable[[], Awaitable[str]]
ClientCallback = Callable[[Any | None], Awaitable[None] | None]

class AuthenticationController:
    """Keep exactly one Blink OAuth challenge alive and swap clients on success.

    A deliberate browser reauthentication is transactional: the current Blink
    client remains active until the candidate has authenticated and persisted
    its cache. Failed, expired, cancelled, and stale attempts never replace it.
    """

    def __init__(
        self,
        config: dict[str, Any],
        config_base: Path,
        *,
        startup_pin: str | None = None,
        client_factory: ClientFactory | None = None,
        legacy_pin_waiter: PinWaiter | None = None,
        on_client: ClientCallback | None = None,
        timeout_seconds: float = AUTH_TIMEOUT_SECONDS,
    ) -> None:
        if client_factory is None:
            from .blink import BlinkClient

            client_factory = BlinkClient

        self.config = config
        self.config_base = config_base
        self.startup_pin = startup_pin
        self.client_factory = client_factory
        self.legacy_pin_waiter = legacy_pin_waiter
        self.on_client = on_client
        self.timeout_seconds = max(0.01, float(timeout_seconds))

        self.client: Any | None = None
        self.state = "idle"
        self.failure_code: str | None = None
        self.challenge_id: str | None = None
        self.started_at: float | None = None
        self.deadline: float | None = None

        self._task: asyncio.Task[None] | None = None
        self._candidate: Any | None = None
        self._pin_future: asyncio.Future[str] | None = None
        self._pin_submitted = False
        self._allow_legacy_pin = False
        self._lock = asyncio.Lock()
        self._closed = False

    def status(self) -> dict[str, Any]:
        """Return only public state; usernames, secrets, and errors stay private."""
        now = time.monotonic()
        remaining = None
        if self.deadline is not None and self.state in {
            "authenticating",
            "waiting_for_pin",
        }:
            remaining = max(0, int(self.deadline - now))

        message_key = self.failure_code or self.state
        return {
            "state": self.state,
            "message": PUBLIC_MESSAGES.get(message_key, PUBLIC_MESSAGES["failure"]),
            "authenticated": self.client is not None,
            "challenge_id": self.challenge_id
            if self.state in {"authenticating", "waiting_for_pin"}
            else None,
            "expires_in": remaining,
            "can_submit_pin": self.state == "waiting_for_pin",
            "can_start": self._task is None or self._task.done(),
            "can_cancel": self._task is not None and not self._task.done(),
        }

    async def start_startup_login(self) -> dict[str, Any]:
        """Start the cache/env/add-on-compatible login without blocking HTTP."""
        return await self._start_attempt(
            username=None,
            password=None,
            fresh_login=False,
            allow_legacy_pin=True,
        )

    async def start_browser_login(self, username: str, password: str) -> dict[str, Any]:
        """Start a fresh login using credentials received in an authorized body."""
        username = str(username).strip()
        password = str(password)
        if not username or not password or len(username) > 320 or len(password) > 1024:
            raise AuthFlowError("credential_failure")
        return await self._start_attempt(
            username=username,
            password=password,
            fresh_login=True,
            allow_legacy_pin=False,
        )

    async def _start_attempt(
        self,
        *,
        username: str | None,
        password: str | None,
        fresh_login: bool,
        allow_legacy_pin: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Authentication controller is closed")
            if self._task is not None and not self._task.done():
                raise AuthConflictError("A Blink authentication attempt is already active")

            self.state = "authenticating"
            self.failure_code = None
            self.challenge_id = secrets.token_urlsafe(24)
            self.started_at = time.monotonic()
            self.deadline = self.started_at + self.timeout_seconds
            self._pin_future = asyncio.get_running_loop().create_future()
            self._pin_submitted = False
            self._allow_legacy_pin = allow_legacy_pin
            challenge_id = self.challenge_id
            self._task = asyncio.create_task(
                self._run_attempt(
                    challenge_id,
                    username=username,
                    password=password,
                    fresh_login=fresh_login,
                ),
                name="blink-authentication",
            )

        # Give a cached/fake login one scheduling turn to expose its real state.
        await asyncio.sleep(0)
        return self.status()

    def _set_client_state(self, state: str) -> None:
        if state not in AUTH_STATES:
            return
        if state == "waiting_for_pin":
            self.state = state

    async def _wait_for_pin(self) -> str:
        future = self._pin_future
        if future is None:
            raise AuthFlowError("expired")

        waiters: set[asyncio.Future[Any] | asyncio.Task[Any]] = {future}
        legacy_task: asyncio.Task[str] | None = None
        if self._allow_legacy_pin and self.legacy_pin_waiter is not None:
            legacy_task = asyncio.create_task(
                self.legacy_pin_waiter(), name="blink-addon-pin-waiter"
            )
            waiters.add(legacy_task)

        remaining = max(0.0, (self.deadline or time.monotonic()) - time.monotonic())
        done, pending = await asyncio.wait(
            waiters,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for waiter in pending:
            if waiter is not future:
                waiter.cancel()
        if legacy_task is not None and legacy_task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await legacy_task

        if not done:
            raise AuthFlowError("expired")

        winner = next(iter(done))
        pin = str(winner.result() or "").strip()
        if not pin:
            raise AuthFlowError("expired")
        self._pin_submitted = True
        self.state = "authenticating"
        return pin

    async def submit_pin(self, challenge_id: str, pin: str) -> dict[str, Any]:
        """Deliver a fresh PIN only to the exact in-memory challenge that asked."""
        pin = str(pin).strip()
        async with self._lock:
            if (
                self.state != "waiting_for_pin"
                or not self.challenge_id
                or not secrets.compare_digest(str(challenge_id), self.challenge_id)
                or self._pin_future is None
                or self._pin_future.done()
            ):
                raise StaleChallengeError("The Blink challenge is no longer active")
            if not pin.isdigit() or not 4 <= len(pin) <= 10:
                raise AuthFlowError("invalid_pin")
            self._pin_future.set_result(pin)
            self.state = "authenticating"

        await asyncio.sleep(0)
        return self.status()

    async def cancel(self, challenge_id: str) -> dict[str, Any]:
        """Cancel only the matching attempt and leave the active client untouched."""
        async with self._lock:
            if (
                self._task is None
                or self._task.done()
                or not self.challenge_id
                or not secrets.compare_digest(str(challenge_id), self.challenge_id)
            ):
                raise StaleChallengeError("The Blink challenge is no longer active")
            task = self._task
            task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task
        return self.status()

    async def _run_attempt(
        self,
        challenge_id: str,
        *,
        username: str | None,
        password: str | None,
        fresh_login: bool,
    ) -> None:
        candidate = self.client_factory(
            self.config,
            self.config_base,
            self.startup_pin,
            username=username,
            password=password,
            pin_provider=self._wait_for_pin,
            state_callback=self._set_client_state,
            fresh_login=fresh_login,
            # A browser attempt must not touch the cache a working session is
            # still using. The startup attempt has no session to protect, and
            # needs its pre-2FA hardware_id on disk so an interrupted first
            # login can still be answered after a restart.
            persist_intermediate=not fresh_login,
        )
        self._candidate = candidate
        # Do not retain the request-body strings in controller locals longer
        # than construction requires. The candidate scrubs its ephemeral
        # password from BlinkPy state when the attempt finishes.
        username = None
        password = None

        try:
            await candidate.start()
        except asyncio.CancelledError:
            await candidate.close()
            if self.challenge_id == challenge_id:
                self.state = "idle"
                self.failure_code = None
            raise
        except AuthFlowError as err:
            await candidate.close()
            if self.challenge_id == challenge_id:
                self.failure_code = err.code
                self.state = "expired" if err.code == "expired" else "failure"
            LOGGER.warning("Blink authentication ended with %s", err.code)
        except Exception as err:  # noqa: BLE001 - upstream errors must be redacted
            await candidate.close()
            if self.challenge_id == challenge_id:
                self.failure_code = "invalid_pin" if self._pin_submitted else "credential_failure"
                self.state = "failure"
            # Exception text can contain request data in upstream libraries.
            LOGGER.warning(
                "Blink authentication failed (%s); details redacted",
                type(err).__name__,
            )
        else:
            previous = self.client
            self.client = candidate
            if self.on_client is not None:
                result = self.on_client(candidate)
                if inspect.isawaitable(result):
                    await result
            if previous is not None and previous is not candidate:
                await previous.close()
            if self.challenge_id == challenge_id:
                self.failure_code = None
                self.state = "success"
        finally:
            if self.challenge_id == challenge_id:
                self.challenge_id = None
                self.deadline = None
                self._pin_future = None
                self._candidate = None
                self._allow_legacy_pin = False
                self._task = None

    async def close(self) -> None:
        """Cancel an in-memory challenge and close all owned Blink sessions."""
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        candidate = self._candidate
        if candidate is not None and candidate is not self.client:
            await candidate.close()
        if self.client is not None:
            await self.client.close()
        self.client = None
        self._candidate = None
        self._task = None

