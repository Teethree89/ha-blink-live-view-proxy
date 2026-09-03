"""Tests for the browser-token route that lets an open player restart itself.

A player page holds a token that outlives nothing: Home Assistant rotates
camera access tokens on a timer, and the browser tokens minted here expire on
their own TTL. A page left open therefore ends up replaying a dead token, and
before this route existed every Restart came back 403 with no way out but a
reload. These tests pin both halves of the deal — a credentialed caller can
always get a fresh token, and an expired one can never mint its own successor.

No Home Assistant, no Blink account, no network: the Home Assistant modules
views.py imports are stubbed, and the package is loaded by path so the real
__init__ (which does need Home Assistant) stays out of it. Run from the repo
root:

    python tests/test_browser_token.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from typing import Any

from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "blink_liveview_proxy"

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def _stub_home_assistant() -> None:
    """Register the handful of Home Assistant names views.py imports."""

    def module(name: str, *, package: bool = False) -> types.ModuleType:
        mod = types.ModuleType(name)
        if package:
            mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    module("homeassistant", package=True)
    ha_const = module("homeassistant.const")
    ha_const.Platform = types.SimpleNamespace(CAMERA="camera", BINARY_SENSOR="binary_sensor")

    module("homeassistant.components", package=True)
    http_mod = module("homeassistant.components.http")

    class HomeAssistantView:
        """Minimal stand-in; the real base only adds routing we do not use."""

    http_mod.HomeAssistantView = HomeAssistantView
    http_mod.require_admin = lambda handler: handler

    core = module("homeassistant.core")
    core.HomeAssistant = object

    exceptions = module("homeassistant.exceptions")

    class ServiceNotFound(Exception):
        pass

    exceptions.ServiceNotFound = ServiceNotFound

    module("homeassistant.helpers", package=True)
    helpers_http = module("homeassistant.helpers.http")
    helpers_http.KEY_AUTHENTICATED = "ha_authenticated"


def _load_views() -> types.ModuleType:
    """Load views.py under a synthetic package, bypassing the real __init__."""
    _stub_home_assistant()

    package_name = "blink_liveview_proxy_under_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    def load(name: str) -> types.ModuleType:
        full = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(full, COMPONENT / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        return mod

    for name in ("const", "api", "failures", "playlist"):
        load(name)
    return load("views")


views = _load_views()
KEY_AUTHENTICATED = "ha_authenticated"
CAMERA_SLUG = "driveway"
OTHER_SLUG = "front_door"


class FakeCoordinator:
    def __init__(self, slugs: list[str]) -> None:
        self.data = {"cameras": [{"slug": slug, "entity_id": f"camera.{slug}"} for slug in slugs]}


class FakeState:
    def __init__(self, slug: str, access_token: str) -> None:
        self.attributes = {"proxy_slug": slug, "access_token": access_token}


class FakeStates:
    def __init__(self, states: list[FakeState]) -> None:
        self._states = states

    def async_all(self, _domain: str) -> list[FakeState]:
        return list(self._states)


class FakeHass:
    """Just enough hass for the token route: runtime data plus camera states."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self.data: dict[str, Any] = {
            views.DOMAIN: {
                "entry_id": {"coordinator": FakeCoordinator(list(tokens)), "stream_seconds": 60}
            }
        }
        self._states = {slug: FakeState(slug, token) for slug, token in tokens.items()}
        self.states = FakeStates(list(self._states.values()))

    def rotate(self, slug: str, new_token: str) -> None:
        """Mimic Home Assistant rotating a camera access token."""
        self._states[slug].attributes["access_token"] = new_token


class FakeRequest:
    def __init__(self, token: str = "", *, authenticated: bool = False) -> None:
        self.query = {"token": token} if token else {}
        self._data = {KEY_AUTHENTICATED: authenticated}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def mint(hass: FakeHass, request: FakeRequest, slug: str = CAMERA_SLUG) -> web.Response:
    view = views.BlinkLiveviewProxyBrowserTokenView(hass)
    return asyncio.run(view.get(request, slug))


def body_of(response: web.Response) -> dict[str, Any]:
    return json.loads(response.body.decode())


def authorizes(hass: FakeHass, token: str, slug: str = CAMERA_SLUG) -> bool:
    """Whether a media request carrying this token would be let through."""
    try:
        views._authorize_browser_request(hass, FakeRequest(token), slug)
    except web.HTTPForbidden:
        return False
    return True


def test_minting() -> None:
    print("\nminting a browser token")
    hass = FakeHass({CAMERA_SLUG: "camera-token-1", OTHER_SLUG: "other-token-1"})

    response = mint(hass, FakeRequest(authenticated=True))
    payload = body_of(response)
    token = payload["token"]

    check(bool(token), "an authenticated Home Assistant caller receives a token")
    check(
        token != "camera-token-1",
        "the minted token is a scoped browser token, not the camera access token",
    )
    check(
        payload["expires_in"] == views.BROWSER_TOKEN_TTL_SECONDS,
        "the response states how long the token lasts",
    )
    check(
        response.headers.get("Cache-Control") == "no-store",
        "the token response is never cached",
    )
    check(
        "camera-token-1" not in response.body.decode(),
        "the camera access token does not leak into the response",
    )
    check(authorizes(hass, token), "the minted token authorizes a stream request")
    check(
        not authorizes(hass, token, OTHER_SLUG),
        "a token minted for one camera does not open another",
    )


def test_the_bug_restart_used_to_hit() -> None:
    print("\nthe stale-token failure Restart used to hit")
    hass = FakeHass({CAMERA_SLUG: "camera-token-1"})

    check(
        authorizes(hass, "camera-token-1"),
        "the camera access token works while it is current",
    )
    hass.rotate(CAMERA_SLUG, "camera-token-2")
    check(
        not authorizes(hass, "camera-token-1"),
        "once Home Assistant rotates it, the page's baked-in token is refused",
    )

    fresh = body_of(mint(hass, FakeRequest(authenticated=True)))["token"]
    check(authorizes(hass, fresh), "a freshly minted token restores the stream")
    check(
        authorizes(hass, fresh),
        "and keeps working, because each use slides its expiry forward",
    )


def test_who_may_mint() -> None:
    print("\nwho may mint a token")
    hass = FakeHass({CAMERA_SLUG: "camera-token-1"})

    check(
        bool(body_of(mint(hass, FakeRequest("camera-token-1")))["token"]),
        "the camera's current access token can be traded for a browser token",
    )

    stale = FakeRequest("camera-token-1")
    hass.rotate(CAMERA_SLUG, "camera-token-2")
    try:
        mint(hass, stale)
        refused = False
    except web.HTTPForbidden:
        refused = True
    check(refused, "a rotated-out camera token cannot mint a replacement")

    try:
        mint(hass, FakeRequest())
        refused = False
    except web.HTTPForbidden:
        refused = True
    check(refused, "an anonymous caller with no token is refused")

    live = body_of(mint(hass, FakeRequest(authenticated=True)))["token"]
    views._browser_tokens(hass)[live]["expires_at"] = 0
    try:
        mint(hass, FakeRequest(live))
        refused = False
    except web.HTTPForbidden:
        refused = True
    check(refused, "an expired browser token cannot mint its own successor")
    check(
        live not in views._browser_tokens(hass),
        "the expired token is pruned rather than left in memory",
    )


def test_unknown_camera() -> None:
    print("\nunknown cameras")
    hass = FakeHass({CAMERA_SLUG: "camera-token-1"})
    try:
        mint(hass, FakeRequest(authenticated=True), "not-a-camera")
        rejected = False
    except web.HTTPNotFound:
        rejected = True
    check(rejected, "an unknown slug is a 404 before any token work happens")


def test_route_is_registered() -> None:
    print("\nroute wiring")
    view = views.BlinkLiveviewProxyBrowserTokenView
    check(
        view.url == "/api/blink_liveview_proxy/cameras/{slug}/token",
        "the token route sits alongside the other per-camera routes",
    )
    check(
        view.requires_auth is False,
        "the route is reachable without a Home Assistant session, like the player",
    )

    source = (COMPONENT / "views.py").read_text()
    check(
        "hass.http.register_view(BlinkLiveviewProxyBrowserTokenView(hass))" in source,
        "the view is registered on setup",
    )


def main() -> int:
    test_minting()
    test_the_bug_restart_used_to_hit()
    test_who_may_mint()
    test_unknown_camera()
    test_route_is_registered()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for name in FAILURES:
            print(f"  {name}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
