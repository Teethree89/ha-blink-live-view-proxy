"""Reach Lovelace's resource list across the three shapes it has had.

`hass.data["lovelace"]` has been three different objects inside this project's
supported range:

  * 2024.6 to 2025.1 — a plain dict, with "mode" and "resources" keys.
  * 2025.2 to 2026.2 — a LovelaceData dataclass, with `mode` and `resources`.
  * 2026.3 onwards — the same dataclass, plus `resource_mode`, which is the
    field that actually decides whether resources are writable. A storage-mode
    dashboard can take its resources from YAML, and only this says so.

Reading it with `getattr(lovelace, "resource_mode")` alone finds nothing on the
first two, which is how the dialog resource came to be registered on 2026.3 and
newer only — everywhere else the registration quietly decided Lovelace was in
YAML mode and returned. That is the exact silent failure the registration
exists to prevent, and the panel now reports on.

No Home Assistant imports: this is attribute archaeology, and it is the part
worth testing.
"""

from __future__ import annotations

from typing import Any

# What Lovelace calls a resource list it will let us write to.
MODE_STORAGE = "storage"


def resource_collection(lovelace: Any) -> Any:
    """The collection holding Lovelace's resources, or None if unreachable."""
    if lovelace is None:
        return None
    if isinstance(lovelace, dict):
        return lovelace.get("resources")
    return getattr(lovelace, "resources", None)


def resource_mode(lovelace: Any) -> str | None:
    """Where Lovelace's resources come from, or None when it does not say.

    `resource_mode` wins wherever it exists, because from 2026.3 it can differ
    from `mode`. Older cores have only `mode`, which governed both.
    """
    if lovelace is None:
        return None
    if isinstance(lovelace, dict):
        value = lovelace.get("resource_mode") or lovelace.get("mode")
        return str(value) if value else None
    for name in ("resource_mode", "mode"):
        value = getattr(lovelace, name, None)
        if value:
            return str(value)
    return None


def is_writable(lovelace: Any) -> bool:
    """Whether resources can be added, rather than read out of configuration.yaml.

    An unreported mode is treated as not writable. Attempting a write into a
    YAML collection raises where it is caught and logged as a failure, while
    declining to write is recoverable by hand and says so.
    """
    return resource_mode(lovelace) == MODE_STORAGE
