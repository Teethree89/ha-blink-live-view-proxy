"""Decide whether the proxy is too old for what this integration expects.

The two halves release together but update independently — HACS moves the
integration, and nothing moves the proxy — so an integration talking to an
older proxy is normal, not a misconfiguration. It only matters when the
integration needs a route that proxy has never had, and the failure then looks
like a 404 in a panel rather than anything about versions.

Two questions, both answered from /status alone: whether the proxy is behind,
and whether this install has any way to do something about it. The second one
decides whether the user is offered a button or a paragraph.

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
        parsed = tuple(int(part) for part in parts)
    except ValueError:
        return None
    if any(part < 0 for part in parsed):
        return None
    # Dotted versions commonly omit trailing zeroes. Without normalizing,
    # Python would sort 0.5 below 0.5.0 even though they name the same release.
    return parsed + (0,) * (4 - len(parsed))


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


def is_behind(reported: str | None, current: str | None) -> bool:
    """True when the proxy is older than this integration, both versions known.

    Unlike is_outdated, an unreadable version is not "behind". That case is the
    floor check's already, and guessing here would offer an update to someone
    whose proxy may well be newer than anything this build can recognise.
    """
    running = parse_version(current)
    found = parse_version(reported)
    if running is None or found is None:
        return False
    return found < running


# Set by the proxy when it is running as a Home Assistant add-on. It cannot
# update itself there - but Home Assistant can ask Supervisor to, so this is
# the one "unsupported" answer that still earns a button.
UPDATE_METHOD_SUPERVISOR = "supervisor"


def update_support(status: dict | None) -> dict:
    """The proxy's account of how it gets new code, or an empty answer.

    Absent on an older proxy, and absent for an unauthorized caller, so callers
    have to cope with knowing nothing - which reads the same as "no button".
    """
    if not isinstance(status, dict):
        return {}
    support = status.get("update")
    return support if isinstance(support, dict) else {}


def update_method(status: dict | None) -> str | None:
    """How the proxy says it gets updated, or None when it does not say."""
    method = update_support(status).get("method")
    return method if isinstance(method, str) and method else None


def can_start_update(status: dict | None) -> bool:
    """Whether there is anything here for a Fix button to press.

    Either the proxy runs its own updater, or it is the add-on and Supervisor
    runs one for it. A container, or a host install with no updater unit, gets
    the notice and the instructions without a button that would only fail.
    """
    support = update_support(status)
    return bool(support.get("supported")) or (
        support.get("method") == UPDATE_METHOD_SUPERVISOR
    )


def update_blocker(status: dict | None) -> str | None:
    """The proxy's own words for why it cannot update itself, if it said."""
    reason = update_support(status).get("reason")
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


# The two notices this integration can raise, named by their translation keys.
NOTICE_OUTDATED = "proxy_outdated"
NOTICE_OUTDATED_FIXABLE = "proxy_outdated_fixable"
NOTICE_BEHIND = "proxy_behind"


def review(
    reported: str | None,
    own_version: str | None,
    minimum: str,
    status: dict | None,
) -> str | None:
    """Which notice a proxy earns, if any.

    Below the floor is a real fault and is always said, button or no button.
    Merely trailing this release is only said where it can be acted on: that
    proxy works, and a notice nobody can clear is noise rather than news.
    """
    if is_outdated(reported, minimum):
        return NOTICE_OUTDATED
    if can_start_update(status) and is_behind(reported, own_version):
        return NOTICE_BEHIND
    return None
