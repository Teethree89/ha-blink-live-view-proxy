"""Turn a failed proxy authentication call into something actionable.

The panel used to render one message for every failure — "check the proxy URL
and API token" — which was wrong in the two cases people actually hit: a proxy
older than the /auth routes, and a proxy running without a token. Both are
reachable, correctly configured proxies, so the advice sent people to look at
the one thing that was not broken.

No Home Assistant imports here on purpose: the mapping is the part worth
testing, and it should be testable without a Home Assistant install.
"""

from __future__ import annotations

from typing import Any

UNREACHABLE = "proxy_unreachable"
OUTDATED = "proxy_outdated"
NO_TOKEN = "proxy_token_missing"
BAD_TOKEN = "proxy_token_mismatch"
UNKNOWN = "proxy_error"

# What the proxy answered, what that means, and the command that fixes it.
# Remedies are shown as text for a human to run: Home Assistant cannot reach a
# systemd unit on the host, and pretending otherwise would be a button that
# silently does nothing.
_FAILURES: dict[str, dict[str, str]] = {
    UNREACHABLE: {
        "message": (
            "Home Assistant could not reach the proxy at all. Check that the "
            "service is running and that the configured proxy URL is right."
        ),
        "remedy": "systemctl status blink-liveview-proxy.service",
    },
    OUTDATED: {
        "message": (
            "The proxy answered, but it has no /auth routes — it predates "
            "browser authentication. Upgrade the proxy, then reload this page."
        ),
        "remedy": (
            "# on the proxy host, from a checkout of this repo\n"
            "sudo scripts/install-proxy.sh\n"
            "# add-on installs upgrade themselves from the add-on store"
        ),
    },
    NO_TOKEN: {
        "message": (
            "The proxy is running without an API token, so it refuses browser "
            "authentication. Give it a token, restart it, and enter the same "
            "token in this integration."
        ),
        "remedy": (
            "# on the proxy host\n"
            "printf 'BLINK_PROXY_TOKEN=%s\\n' \"$(openssl rand -hex 32)\" \\\n"
            "  | sudo tee /etc/blink-liveview-proxy/blink-liveview-proxy.env >/dev/null\n"
            "sudo chmod 600 /etc/blink-liveview-proxy/blink-liveview-proxy.env\n"
            "sudo systemctl restart blink-liveview-proxy.service\n"
            "sudo sed -n 's/^BLINK_PROXY_TOKEN=//p' \\\n"
            "  /etc/blink-liveview-proxy/blink-liveview-proxy.env"
        ),
    },
    BAD_TOKEN: {
        "message": (
            "The proxy rejected this integration's token. Update it in the "
            "integration's options, or accept the reauthentication prompt."
        ),
        "remedy": "Settings -> Devices & services -> Blink Live View Proxy -> Configure",
    },
    UNKNOWN: {
        "message": (
            "The proxy could not complete the authentication request. Check the "
            "proxy log for what it was doing."
        ),
        "remedy": "journalctl -u blink-liveview-proxy -n 50",
    },
}


def classify(status: int | None) -> str:
    """Name the failure from the proxy's status code alone.

    404 is the tell for an old proxy: the route does not exist. 503 is the new
    proxy saying it has no token configured, which is deliberate and not an
    error to debug. 401/403 mean the token this integration holds is wrong.
    """
    if status is None:
        return UNREACHABLE
    if status == 404:
        return OUTDATED
    if status == 503:
        return NO_TOKEN
    if status in (401, 403):
        return BAD_TOKEN
    return UNKNOWN


def failure_payload(status: int | None) -> dict[str, Any]:
    """Build the panel's failure state. Carries no upstream text, ever."""
    reason = classify(status)
    detail = _FAILURES[reason]
    return {
        "state": "failure",
        "reason": reason,
        "message": detail["message"],
        "remedy": detail["remedy"],
        "authenticated": False,
        "challenge_id": None,
        "expires_in": None,
        "can_submit_pin": False,
        "can_start": False,
        "can_cancel": False,
    }
