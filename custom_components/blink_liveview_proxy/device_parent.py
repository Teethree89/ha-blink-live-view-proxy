"""How a camera's device names the proxy device as its parent.

Home Assistant is moving from `via_device`, an identifier tuple, to
`via_device_id`, a device registry id. The newer key is the one to use where
it exists: `via_device` logs a deprecation warning there and stops working in
2027.8. But it does not exist everywhere this integration installs. Up to at
least 2026.5, DeviceRegistry.async_get_or_create() has no such parameter, and
Home Assistant passes device_info straight into it, so every entity carrying
the new key fails with a TypeError and no camera is created at all.

So the key is chosen by asking the registry which one it takes, once, rather
than by comparing version numbers.
"""

from __future__ import annotations

import inspect
from functools import cache
from typing import Any

from homeassistant.helpers import device_registry as dr

# The only place the old key may be spelled; tests/test_via_device_id.py
# holds every other file to via_device_id.
_LEGACY_PARENT_KEY = "via_device"


@cache
def registry_takes_via_device_id() -> bool:
    """Whether this Home Assistant's device registry accepts via_device_id."""
    try:
        parameters = inspect.signature(dr.DeviceRegistry.async_get_or_create).parameters
    except (TypeError, ValueError):
        return True
    return "via_device_id" in parameters


def parent_device_info(hub_device_id: str, hub_identifier: tuple[str, str]) -> dict[str, Any]:
    """The device_info entry that hangs a device off the proxy device."""
    if registry_takes_via_device_id():
        return {"via_device_id": hub_device_id}
    return {_LEGACY_PARENT_KEY: hub_identifier}
