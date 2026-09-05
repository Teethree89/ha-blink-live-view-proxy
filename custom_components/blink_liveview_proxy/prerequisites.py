"""What has to be in place before any of this works, and how to put it there.

The dashboard's Overview reads this. Every check has three possible answers,
not two — met, not met, and "this install cannot be asked" — because guessing
is how a working setup gets told to fix something that is not broken. A proxy
too old to report its environment is the common case there, and it is not a
fault.

The interesting failures on this list are all silent. A missing Lovelace
resource makes every tile do nothing at all: no console error, no log line, no
failed request. A missing official Blink integration shows up only as one
button returning a 404. A blinkpy a few releases back reports a failed login
while Blink texts the code anyway. None of those announce themselves, so the
panel says them out loud instead.

The instructions belong to the check, not to its failure, and the panel renders
them either way. Someone whose install is entirely green still needs them when
they rebuild it on a new host, and a "how to fix" that only exists while broken
is one nobody can read ahead of time.

No Home Assistant imports: views.py gathers the facts, and the decisions are
the part worth testing.
"""

from __future__ import annotations

from typing import Any

try:  # The package's own module, when Home Assistant imported us normally.
    from .version_check import parse_version
except ImportError:  # pragma: no cover - the tests load this file on its own.
    from version_check import parse_version  # type: ignore[no-redef]

# Met, unmet, and unanswerable. The third is not a failure and must not be
# coloured like one.
OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"

DOCS_BASE = "https://github.com/Teethree89/ha-blink-live-view-proxy/blob/main/docs"

# Where the HACS frontend cards land. Their resource URL is what proves they
# are installed, and HACS names the folder after the repository.
_CARD_MARKERS = {
    "button-card": "button-card",
    "auto-entities": "auto-entities",
}


def _row(
    key: str,
    label: str,
    state: str,
    detail: str,
    needed_for: str,
    required: bool,
    instructions: list[str],
    docs_url: str = "",
) -> dict[str, Any]:
    """One line of the readout, in the shape the panel renders."""
    return {
        "key": key,
        "label": label,
        "state": state,
        "detail": detail,
        "needed_for": needed_for,
        "required": required,
        "instructions": instructions,
        "docs_url": docs_url,
    }


def _home_assistant(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether the core is new enough for what HACS agreed to install."""
    floor = str(facts.get("minimum_ha") or "")
    running = str(facts.get("ha_version") or "")
    parsed, wanted = parse_version(running), parse_version(floor)

    if wanted is None or parsed is None:
        # Betas and dev builds are not plain dotted numbers. Nothing is wrong;
        # the comparison just cannot be made, and saying so beats a red row.
        state, detail = UNKNOWN, (
            f"Running {running or 'an unreported version'}, which is not a plain "
            f"version number, so it cannot be compared to {floor}."
        )
    elif parsed >= wanted:
        state, detail = OK, f"Running {running}."
    else:
        state, detail = MISSING, (
            f"Running {running}. This integration is built against {floor} and "
            "newer; older cores are untested here."
        )

    return _row(
        "home_assistant",
        f"Home Assistant {floor} or newer",
        state,
        detail,
        "Everything",
        True,
        [
            f"{floor} is the floor hacs.json enforces, so HACS refuses to "
            "install this integration below it.",
            "Settings → System → Updates installs core updates.",
        ],
    )


def _blink_integration(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether the official Blink integration is set up and answering.

    Recommended, never required. This project talks to Blink itself and shares
    nothing with that integration but the account, so its absence costs three
    named features and breaks nothing.
    """
    entries = int(facts.get("blink_entries") or 0)
    loaded = int(facts.get("blink_loaded") or 0)
    has_service = bool(facts.get("blink_service"))

    if loaded and has_service:
        state, detail = OK, (
            f"Set up and loaded ({loaded} "
            f"{'entry' if loaded == 1 else 'entries'}); blink.trigger_camera "
            "is available."
        )
    elif loaded:
        state, detail = MISSING, (
            "Loaded, but blink.trigger_camera is not registered. Snapshot "
            "refresh calls that action and will return a 404 until it is."
        )
    elif entries:
        state, detail = MISSING, (
            f"Installed but not loaded ({entries} "
            f"{'entry' if entries == 1 else 'entries'} in an error or retry "
            "state). Re-authenticate it under Settings → Devices & services."
        )
    else:
        state, detail = MISSING, (
            "Not set up. Live view, clips, push-to-talk, the direct player and "
            "this panel all work without it."
        )

    return _row(
        "blink_integration",
        "Official Blink integration",
        state,
        detail,
        "Snapshot refresh, motion switches, battery and temperature sensors",
        False,
        [
            "Settings → Devices & services → Add integration → Blink, signed "
            "in to the same Blink account.",
            "Then point each camera in the proxy's camera map at the "
            "camera.* entity it creates, so the snapshot features find it.",
            "The two log in separately, with their own device ids and refresh "
            "tokens, so re-authenticating one does nothing to the other. "
            "Blink's rate limits are per account, though: a reload loop on "
            "either can exhaust them and make the other's next login fail.",
        ],
        f"{DOCS_BASE}/INSTALL.md#alongside-the-official-blink-integration",
    )


def _unreported(facts: dict[str, Any]) -> str:
    """Why the proxy said nothing about its environment."""
    since = str(facts.get("environment_proxy_version") or "a newer release")
    running = str(facts.get("proxy_version") or "")
    return (
        f"This proxy does not report its environment. That arrived in proxy "
        f"{since}, and this one "
        + (f"reports {running}." if running else "did not say which version it is.")
    )


def _blinkpy(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether the proxy imports the exact blinkpy this release was tested on."""
    wanted = str(facts.get("required_blinkpy") or "")
    environment = facts.get("environment")

    if not isinstance(environment, dict):
        state, detail = UNKNOWN, _unreported(facts)
    elif not environment.get("blinkpy"):
        state, detail = MISSING, (
            "The proxy cannot find blinkpy installed at all. Nothing that "
            "reaches Blink works without it."
        )
    elif str(environment["blinkpy"]) == wanted:
        state, detail = OK, f"{wanted}, the pinned version."
    else:
        state, detail = MISSING, (
            f"The proxy has blinkpy {environment['blinkpy']}; this release "
            f"pins {wanted}."
        )

    return _row(
        "blinkpy",
        f"blinkpy {wanted} on the proxy",
        state,
        detail,
        "Login, camera discovery, live view, clips",
        True,
        [
            "The pin is exact, not a range, and on purpose: 0.25.5 reads "
            "Blink's current 2FA challenge as a failed login while the code is "
            "texted to you anyway.",
            "Add-on and Docker installs build it into the image; updating the "
            "add-on or pulling the image is the whole fix.",
            "systemd host: sudo /opt/blink-liveview-proxy/.venv/bin/python -m "
            "pip install -r /opt/blink-liveview-proxy/requirements.txt, then "
            "sudo systemctl restart blink-liveview-proxy.",
        ],
        f"{DOCS_BASE}/INSTALL.md",
    )


def _ffmpeg(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether the binary the proxy shells out to is where it expects."""
    environment = facts.get("environment")

    if not isinstance(environment, dict):
        state, detail = UNKNOWN, _unreported(facts)
    elif not environment.get("ffmpeg"):
        state, detail = MISSING, (
            "The proxy cannot find ffmpeg. Live view, push-to-talk and the "
            "clip remux all shell out to it, and each fails on its own."
        )
    else:
        state, detail = OK, str(environment["ffmpeg"])

    return _row(
        "ffmpeg",
        "ffmpeg on the proxy host",
        state,
        detail,
        "Live view, push-to-talk, downloadable clips",
        True,
        [
            "Add-on and Docker installs ship it inside the image; there is "
            "nothing to do.",
            "systemd host: sudo apt install ffmpeg. The one-line installer "
            "already does this on apt systems.",
            "Somewhere non-standard: set ffmpeg in the proxy's config.json to "
            "the full path.",
        ],
        f"{DOCS_BASE}/CONFIGURATION.md",
    )


def _dashboard_resource(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether the dialog module is in Lovelace's resource list.

    The one on this list with no symptom whatsoever when it is wrong, which is
    exactly why it earns a row of its own.
    """
    url = str(facts.get("resource_url") or "")
    legacy_url = str(facts.get("legacy_resource_url") or "")
    urls = facts.get("resource_urls")
    mode = str(facts.get("lovelace_mode") or "")
    # Registered entries may carry a cache-busting query string from HACS.
    registered = [str(item).split("?", 1)[0] for item in urls or []]

    if not isinstance(urls, list):
        state, detail = UNKNOWN, (
            "Lovelace has not started, or its resource list cannot be read "
            "from here."
        )
    elif url in registered:
        state, detail = OK, "Registered as a JavaScript module."
    elif legacy_url and legacy_url in registered:
        # Working, but on the path Home Assistant's service worker caches
        # forever. Not a fault to report; a move to make where we are allowed.
        state, detail = OK, (
            "Registered, on the path used before 0.6.2. It is still served, "
            "but Home Assistant caches that one indefinitely over HTTPS. The "
            "integration moves it wherever Lovelace can be written to."
        )
    elif mode == "yaml":
        state, detail = MISSING, (
            "Not registered, and Lovelace is in YAML mode, so the integration "
            "cannot add it. This one has to go in configuration.yaml."
        )
    else:
        state, detail = MISSING, (
            "Not in the resource list. Every live view, clips and snapshot "
            "button will do nothing when tapped."
        )

    return _row(
        "dashboard_resource",
        "Lovelace dialog resource",
        state,
        detail,
        "Every button on a generated dashboard",
        True,
        [
            "The integration adds this itself on every setup, wherever "
            "Lovelace is in storage mode. Normally there is nothing to do.",
            f"By hand: Settings → Dashboards → ⋮ → Resources → Add resource, "
            f"URL {url}, type JavaScript module.",
            "YAML-mode Lovelace cannot be written to, so add it under "
            "lovelace: resources: in configuration.yaml instead.",
            "When it is missing the failure is silent — no console error, no "
            "log line, no failed request. If a tap does nothing at all, check "
            "this before anything else.",
            "Home Assistant only loads Lovelace resources on a dashboard, "
            "never on this panel, so this row says whether it is registered — "
            "not whether it is loaded on the page you are reading.",
        ],
        f"{DOCS_BASE}/DASHBOARD.md#the-dashboard-resource",
    )


def _frontend_card(facts: dict[str, Any], name: str) -> dict[str, Any]:
    """Whether a HACS frontend card the example dashboards need is installed."""
    urls = facts.get("resource_urls")
    self_populating = name == "auto-entities"

    if not isinstance(urls, list):
        state, detail = UNKNOWN, (
            "Lovelace has not started, or its resource list cannot be read "
            "from here."
        )
    elif any(_CARD_MARKERS[name] in str(item) for item in urls):
        state, detail = OK, "Found in the Lovelace resource list."
    else:
        state, detail = MISSING, (
            "Not in the Lovelace resource list. "
            + (
                "Only the self-populating dashboard uses it; the generated "
                "ones do not."
                if self_populating
                else "Every generated dashboard, view and card uses it, and "
                "without it each tile renders as an error card."
            )
        )

    return _row(
        name.replace("-", "_"),
        name,
        state,
        detail,
        (
            "The self-populating example dashboard only"
            if self_populating
            else "Every dashboard this project generates"
        ),
        False,
        [
            f"HACS → Frontend → search {name} → Download, then reload the "
            "browser with a hard refresh.",
            "HACS adds the Lovelace resource for you; this check reads that "
            "same list, so a card installed another way may not show up here "
            "even though it works.",
        ],
        f"{DOCS_BASE}/DASHBOARD.md#requirements-at-a-glance",
    )


def _integration_update(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether HACS is holding a newer release of this integration.

    The one question the integration cannot answer about itself: the only
    version it can see is the one it is running. HACS already tracks the
    repository and publishes an update entity, so this reads that rather than
    polling GitHub — no network call here, no rate limit to share, and it
    honours whatever release channel the user configured in HACS.

    A copy installed by hand into custom_components/ has no such entity. That
    is unknown, not out of date.
    """
    hacs = facts.get("hacs_update") or {}
    installed = str(facts.get("integration_version") or "")

    if not hacs.get("found"):
        state, detail = UNKNOWN, (
            f"{installed or 'This release'} is installed. HACS is not tracking "
            "this integration here, so there is nothing to compare against."
        )
    elif hacs.get("update_available"):
        state, detail = MISSING, (
            f"HACS has {hacs.get('latest') or 'a newer release'}; "
            f"{hacs.get('installed') or installed} is installed."
        )
    else:
        state, detail = OK, (
            f"{hacs.get('installed') or installed} is the newest release HACS "
            "offers."
        )

    return _row(
        "integration_update",
        "Integration up to date",
        state,
        detail,
        "Fixes and new checks in this panel",
        False,
        [
            "HACS → Integrations → Blink Live View Proxy → Update, then "
            "restart Home Assistant.",
            "The two halves move separately: HACS updates the integration, and "
            "nothing updates the proxy. Use the Update proxy action above "
            "where this install supports it.",
            "This reads the update entity HACS publishes. An integration "
            "copied into custom_components/ by hand has no such entity, and "
            "this row says so rather than guessing.",
        ],
        f"{DOCS_BASE}/INSTALL.md",
    )


def _secure_context(facts: dict[str, Any]) -> dict[str, Any]:
    """Whether the browser reading this page may open a microphone.

    Push-to-talk is the one feature that does not live on the proxy at all
    before it starts: the browser captures the audio, and `getUserMedia` only
    exists in a secure context — HTTPS, or `http://localhost`. Home Assistant's
    own default, `http://<address>:8123`, is not one, and neither is the
    companion app pointed at an internal URL.

    It earns a row because of how it fails. Hold Talk is enabled from whether
    the *camera* supports it, so on plain HTTP the button is offered, looks
    live, and does nothing: the refusal is written to a status line the player
    has already hidden by the time the button becomes usable. Four different
    refusals reach the same invisible place. Nothing is logged proxy-side,
    because nothing was ever sent.

    Only the browser can answer this, so the panel reports what it sees and an
    older panel that does not send it leaves the row unanswered rather than
    guessing. Never required: live view, clips, snapshots and everything else
    are unaffected, and most installs are plain HTTP on purpose.
    """
    reported = facts.get("secure_context")

    if reported is None:
        state, detail = UNKNOWN, (
            "The page did not report whether it is a secure context. A panel "
            "from before this check existed does not send it."
        )
    elif reported:
        state, detail = OK, (
            "This page is a secure context, so the browser will hand over a "
            "microphone when Hold Talk asks for one."
        )
    else:
        state, detail = MISSING, (
            "This page is not a secure context, so the browser refuses the "
            "microphone and Hold Talk cannot work from this address. It is "
            "still offered on cameras that support it, and pressing it does "
            "nothing visible. Everything else is unaffected."
        )

    return _row(
        "secure_context",
        "HTTPS, or a browser-trusted origin",
        state,
        detail,
        "Push-to-talk",
        False,
        [
            "The address in the browser is what counts, not how the proxy is "
            "reached: Home Assistant's default http://<address>:8123 is not a "
            "secure context, and http://localhost is.",
            "Home Assistant Cloud (Nabu Casa) gives you an HTTPS address with "
            "nothing to configure, and the companion app can use it at home "
            "too — Settings → Companion app → Internal URL, left empty.",
            "Self-hosted: put a reverse proxy with a certificate in front of "
            "Home Assistant, or set ssl_certificate and ssl_key under http: "
            "in configuration.yaml.",
            "Only push-to-talk needs this. Live view, clips, downloads and "
            "snapshots work over plain HTTP.",
        ],
        f"{DOCS_BASE}/CONFIGURATION.md#push-to-talk",
    )


def build(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """The whole readout, in the order it is worth reading.

    Home Assistant first because everything sits on it, then this integration's
    own currency, then the two halves of the account story, then the proxy
    host, then the dashboard layer, and last the browser the page is being read
    in — roughly outward from the thing least likely to be the problem.
    """
    return [
        _home_assistant(facts),
        _integration_update(facts),
        _blink_integration(facts),
        _blinkpy(facts),
        _ffmpeg(facts),
        _dashboard_resource(facts),
        _frontend_card(facts, "button-card"),
        _frontend_card(facts, "auto-entities"),
        _secure_context(facts),
    ]


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Counts for the panel's headline, so it does not recount in JavaScript."""
    return {
        "total": len(rows),
        "ok": sum(1 for row in rows if row["state"] == OK),
        "missing": sum(1 for row in rows if row["state"] == MISSING),
        "unknown": sum(1 for row in rows if row["state"] == UNKNOWN),
        "blocking": sum(
            1 for row in rows if row["state"] == MISSING and row["required"]
        ),
    }
