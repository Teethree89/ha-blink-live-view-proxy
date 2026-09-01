"""HTTP client for the local Blink live-view proxy."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import ClientError, ClientResponseError, ClientSession

REQUEST_TIMEOUT = 10


class ProxyError(Exception):
    """Base proxy API error."""


class ProxyAuthError(ProxyError):
    """The proxy rejected the configured token."""


class ProxyConnectionError(ProxyError):
    """The proxy could not be reached or returned invalid data.

    `status` is the proxy's HTTP status when there was one, and None when the
    request never got an answer. It is what tells an old proxy (404 on the auth
    routes) apart from one deliberately running without a token (503).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def normalize_base_url(value: str) -> str:
    """Normalize a user-entered proxy URL."""
    value = value.strip().rstrip("/")
    if not value:
        raise ProxyConnectionError("Missing proxy base URL")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value


class BlinkLiveviewProxyClient:
    """Small async client for the local proxy API."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        token: str | None = None,
    ) -> None:
        self._session = session
        self.base_url = normalize_base_url(base_url)
        self.token = (token or "").strip()

    async def async_get_health(self) -> dict[str, Any]:
        """Fetch proxy health."""
        return await self._request_json("/health")

    async def async_get_cameras(self) -> list[dict[str, Any]]:
        """Fetch camera metadata from the proxy."""
        data = await self._request_json("/cameras")
        cameras = data.get("cameras")
        if not isinstance(cameras, list):
            raise ProxyConnectionError("Proxy /cameras response did not include a list")
        return cameras

    async def async_get_status(self) -> dict[str, Any]:
        """Fetch the proxy's unauthenticated status, including its version."""
        return await self._request_json("/status")

    async def async_get_auth_status(self) -> dict[str, Any]:
        """Fetch the proxy's public browser-authentication state."""
        return await self._request_json("/auth/status")

    async def async_start_auth(self, username: str, password: str) -> dict[str, Any]:
        """Forward credentials in an authorized request body, never a URL."""
        return await self._request_json(
            "/auth/login",
            method="POST",
            json_body={"username": username, "password": password},
        )

    async def async_submit_auth_pin(
        self, challenge_id: str, pin: str
    ) -> dict[str, Any]:
        """Submit a PIN to the same live proxy challenge."""
        return await self._request_json(
            "/auth/pin",
            method="POST",
            json_body={"challenge_id": challenge_id, "pin": pin},
        )

    async def async_cancel_auth(self, challenge_id: str) -> dict[str, Any]:
        """Cancel the matching proxy challenge."""
        return await self._request_json(
            "/auth/cancel",
            method="POST",
            json_body={"challenge_id": challenge_id},
        )

    def stream_url(self, camera: dict[str, Any]) -> str | None:
        """Return the stream URL HA should give to ffmpeg/stream."""
        stream_url = camera.get("mpegts_url") or camera.get("hls_url")
        slug = camera.get("slug")
        if not stream_url and slug:
            stream_url = f"/cameras/{slug}/mpegts"
        if not stream_url:
            return None
        return self._append_token(self._absolute_url(str(stream_url)))

    def proxy_url(self, path: str, query: dict[str, str] | None = None) -> str:
        """Return an absolute proxy URL for an internal API path."""
        url = self._absolute_url(path)
        if query:
            parts = urlsplit(url)
            merged = dict(parse_qsl(parts.query, keep_blank_values=True))
            merged.update(query)
            url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(merged),
                    parts.fragment,
                )
            )
        return url

    def auth_headers(self) -> dict[str, str]:
        """Return proxy authorization headers for server-side requests."""
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch and decode a JSON proxy endpoint."""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method,
                    self._absolute_url(path),
                    headers=headers,
                    json=json_body,
                ) as response:
                    if response.status in (401, 403):
                        raise ProxyAuthError("Proxy token was rejected")
                    response.raise_for_status()
                    data = await response.json(content_type=None)
        except ProxyAuthError:
            raise
        except (asyncio.TimeoutError, ClientResponseError, ClientError) as err:
            # Name the path and the failure type, never the upstream error text:
            # it can carry the full URL and request detail of an auth call.
            raise ProxyConnectionError(
                f"Proxy request to {path} failed ({type(err).__name__})",
                status=getattr(err, "status", None),
            ) from err
        except ValueError as err:
            raise ProxyConnectionError("Proxy returned invalid JSON") from err

        if not isinstance(data, dict):
            raise ProxyConnectionError("Proxy returned non-object JSON")
        return data

    def _absolute_url(self, path_or_url: str) -> str:
        """Make a proxy path absolute."""
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _append_token(self, url: str) -> str:
        """Append the proxy token as a query value for HLS segment requests."""
        if not self.token:
            return url

        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["token"] = self.token
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )
