"""Camera platform for Blink live-view proxy."""

from __future__ import annotations

import base64
import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature, async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BlinkLiveviewProxyClient
from .const import DOMAIN
from .coordinator import BlinkLiveviewProxyCoordinator

LOGGER = logging.getLogger(__name__)


def _loading_svg(snapshot: tuple[str, bytes] | None = None) -> bytes:
    """Build an SVG loading frame, optionally backed by a camera snapshot."""
    if snapshot is None:
        background = """<defs>
<radialGradient id="g" cx="50%" cy="42%" r="72%">
<stop offset="0%" stop-color="#203244"/>
<stop offset="100%" stop-color="#0b1016"/>
</radialGradient>
</defs>
<rect width="1280" height="720" fill="url(#g)"/>
"""
    else:
        content_type, content = snapshot
        encoded = base64.b64encode(content).decode("ascii")
        background = f"""<image href="data:{content_type};base64,{encoded}" x="0" y="0" width="1280" height="720" preserveAspectRatio="xMidYMid slice"/>
<rect width="1280" height="720" fill="#020617" opacity="0.62"/>
"""

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
{background}
<circle cx="640" cy="315" r="42" fill="none" stroke="#7dd3fc" stroke-width="8" stroke-linecap="round" stroke-dasharray="72 190">
<animateTransform attributeName="transform" type="rotate" from="0 640 315" to="360 640 315" dur="1.1s" repeatCount="indefinite"/>
</circle>
<text x="640" y="410" fill="#f8fafc" font-family="Arial, Helvetica, sans-serif" font-size="42" text-anchor="middle">Starting live view</text>
<text x="640" y="462" fill="#b6c7d6" font-family="Arial, Helvetica, sans-serif" font-size="25" text-anchor="middle">Waiting for Blink video</text>
</svg>""".encode("utf-8")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up live-view cameras from the proxy camera inventory."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    client: BlinkLiveviewProxyClient = runtime["client"]
    coordinator: BlinkLiveviewProxyCoordinator = runtime["coordinator"]
    cameras = coordinator.data.get("cameras", [])

    async_add_entities(
        BlinkLiveviewProxyCamera(coordinator, client, entry, camera)
        for camera in cameras
    )


class BlinkLiveviewProxyCamera(
    CoordinatorEntity[BlinkLiveviewProxyCoordinator], Camera
):
    """Expose one proxy HLS stream as a Home Assistant camera."""

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: BlinkLiveviewProxyCoordinator,
        client: BlinkLiveviewProxyClient,
        entry: ConfigEntry,
        camera: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/svg+xml"
        self._client = client
        self._camera = camera
        slug = str(camera.get("slug") or camera.get("id") or "camera")
        name = str(camera.get("name") or slug.replace("_", " ").title())
        key = str(camera.get("serial") or camera.get("id") or slug)

        self._attr_name = f"Blink Live {name}"
        self._attr_unique_id = f"{entry.entry_id}_{key}_live"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, key)},
            "name": f"Blink {name}",
            "manufacturer": "Blink",
            "model": camera.get("product_type") or camera.get("camera_type"),
            "via_device": (DOMAIN, entry.entry_id),
        }

    @property
    def available(self) -> bool:
        """Return whether the proxy says it is healthy."""
        health: dict[str, Any] = self.coordinator.data.get("health", {})
        return super().available and bool(health.get("ok"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return useful camera metadata from the proxy."""
        return {
            "proxy_slug": self._camera.get("slug"),
            "blink_camera_id": self._camera.get("id"),
            "blink_entity_id": self._camera.get("entity_id"),
            "network_id": self._camera.get("network_id"),
            "camera_type": self._camera.get("camera_type"),
            "product_type": self._camera.get("product_type"),
            "ptt_supported": self._camera.get("ptt_supported"),
        }

    async def stream_source(self) -> str | None:
        """Return the HLS stream URL for HA's stream component."""
        return self._client.stream_url(self._camera)

    async def _async_get_supported_webrtc_provider(self, _fn: Any) -> None:
        """Keep this proxy camera on HLS instead of go2rtc/WebRTC."""
        return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes:
        """Return a darkened source snapshot while live view starts."""
        source_entity_id = self._camera.get("entity_id")
        if source_entity_id and source_entity_id != self.entity_id:
            try:
                image = await async_get_image(
                    self.hass,
                    str(source_entity_id),
                    timeout=5,
                    width=width,
                    height=height,
                )
                return _loading_svg((image.content_type, image.content))
            except Exception as err:  # noqa: BLE001
                LOGGER.debug(
                    "Falling back to generic loading image for %s: %s",
                    self.entity_id,
                    err,
                )
        return _loading_svg()
