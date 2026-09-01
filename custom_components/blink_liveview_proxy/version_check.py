"""Decide whether the proxy is too old for what this integration expects.

The two halves release together but update independently — HACS moves the
integration, and nothing moves the proxy — so an integration talking to an
older proxy is normal, not a misconfiguration. It only matters when the
integration needs a route that proxy has never had, and the failure then looks
like a 404 in a panel rather than anything about versions.

No Home Assistant imports: the comparison is the part worth testing.
"""

from __future__ import annotations

# What a proxy that predates /status reporting a version is called in the UI.
UNKNOWN_VERSION = "an unknown version"


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """Parse a plain dotted version. Anything unexpected is None, not a guess."""
    if not value or not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_outdated(reported: str | None, minimum: str) -> bool:
    """True when the proxy is older than the minimum this integration needs.

    A proxy that reports nothing is outdated by definition: the version field
    arrived in the same release as the routes we are asking about. A version we
    cannot parse is treated the same way — better one clearable notice than a
    silent 404 nobody can explain.
    """
    floor = parse_version(minimum)
    if floor is None:
        return False
    found = parse_version(reported)
    if found is None:
        return True
    return found < floor


# A payload field that only exists from a given release onwards, so a proxy
# that reports no version can still be placed. `auth_state` arrived with
# browser authentication in 0.3.0 — which is also the release that started
# reporting `version`, one commit too late to be in the tag.
_CAPABILITY_FLOOR = (("auth_state", "0.3.0"),)


def infer_version(status: dict | None) -> str | None:
    """Take the version from /status, or the oldest release consistent with it.

    Without this, every correct 0.3.0 install would be told it is older than
    0.3.0, and the notice could not be cleared by upgrading — the thing it asks
    for had already been done.
    """
    if not isinstance(status, dict):
        return None
    reported = status.get("version")
    if parse_version(reported):
        return reported
    for field, floor in _CAPABILITY_FLOOR:
        if field in status:
            return floor
    return None


def describe(reported: str | None) -> str:
    """Name the proxy's version for a human, including when it has none."""
    return reported.strip() if parse_version(reported) else UNKNOWN_VERSION
