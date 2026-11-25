"""Tests for the live-view trace and the diagnostics report built from it.

Issue #91 needed three rounds of screenshots to establish that an iPhone loaded
the playlist and never asked for a segment. These pin that the integration now
records every live-view request, refused ones included, and that the report
says where the path stopped without leaking a token or an address.

The views run for real behind an aiohttp test server; only the add-on is faked.
No Home Assistant, no Blink account, no network. Run from the repo root:

    python tests/test_liveview_trace.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import pathlib
import sys
import types
from typing import Any

from aiohttp import ClientError, web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "blink_liveview_proxy"

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str, detail: Any = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)
        if detail:
            print(f"        {str(detail)[:1500]}")


def _stub_home_assistant() -> None:
    def module(name: str, *, package: bool = False) -> types.ModuleType:
        mod = types.ModuleType(name)
        if package:
            mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod
        return mod

    module("homeassistant", package=True)
    ha_const = module("homeassistant.const")
    ha_const.Platform = types.SimpleNamespace(
        ALARM_CONTROL_PANEL="alarm_control_panel", BINARY_SENSOR="binary_sensor",
        BUTTON="button", CAMERA="camera", SENSOR="sensor", SWITCH="switch",
    )
    module("homeassistant.components", package=True)
    http_mod = module("homeassistant.components.http")

    class HomeAssistantView:
        pass

    http_mod.HomeAssistantView = HomeAssistantView
    http_mod.require_admin = lambda handler: handler
    module("homeassistant.core").HomeAssistant = object
    exceptions = module("homeassistant.exceptions")

    class ServiceNotFound(Exception):
        pass

    exceptions.ServiceNotFound = ServiceNotFound
    module("homeassistant.helpers", package=True)
    module("homeassistant.helpers.http").KEY_AUTHENTICATED = "ha_authenticated"
    loader = module("homeassistant.loader")

    async def async_get_integration(_hass, _domain):
        return types.SimpleNamespace(version="9.9.9-test")

    loader.async_get_integration = async_get_integration


def _load() -> tuple[types.ModuleType, types.ModuleType]:
    _stub_home_assistant()
    package_name = "blink_liveview_proxy_trace_test"
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
    trace = load("liveview_trace")
    return trace, load("views")


trace_mod, views = _load()

SLUG = "living_room"
CAMERA_TOKEN = "camera-token-SECRET-1234567890"
SESSION = "0f6c9a52-1d2b-4c1e-9a77-5d6e7f809a1b"
PLAYLIST = "\n".join([
    "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", "#EXT-X-MEDIA-SEQUENCE:0",
    "#EXTINF:1.000456,", "#EXT-X-PROGRAM-DATE-TIME:2026-11-24T21:10:44.293-0600",
    "segment_00000.ts", "",
])


class FakeState:
    def __init__(self, slug: str = SLUG) -> None:
        self.attributes = {"proxy_slug": slug, "access_token": CAMERA_TOKEN}


class FakeHass:
    def __init__(self) -> None:
        coordinator = types.SimpleNamespace(
            data={"cameras": [{"slug": SLUG, "entity_id": f"camera.{SLUG}"},
                              {"slug": "garage", "entity_id": "camera.garage"}]}
        )
        self.data: dict[str, Any] = {
            views.DOMAIN: {"entry_id": {"coordinator": coordinator, "stream_seconds": 60}}
        }
        self.states = types.SimpleNamespace(async_all=lambda _domain: [FakeState(), FakeState("garage")])
        self.is_running = True


class FakeUpstream:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.status = 200
        self.headers: dict[str, str] = {"Content-Length": str(len(body))}
        self.content = self

    async def text(self) -> str:
        return self.body.decode()

    async def iter_chunked(self, _size: int):
        yield self.body

    def release(self) -> None:
        pass

    def close(self) -> None:
        pass


class RefusingPost:
    async def __aenter__(self):
        raise ClientError("add-on unreachable")

    async def __aexit__(self, *_args):
        return False


# What the fake add-on does, per path. A callable raises; bytes are served.
ADDON: dict[str, Any] = {}


async def fake_open(_client, path: str, _query=None, _headers=None):
    action = ADDON.get(path.rsplit("/", 1)[-1])
    if callable(action):
        action()
    return FakeUpstream(action if isinstance(action, bytes) else b"")


views._open_proxy_response = fake_open
views._client = lambda _hass: types.SimpleNamespace(
    _session=types.SimpleNamespace(post=lambda *a, **k: RefusingPost()),
    proxy_url=lambda path, query=None: path,
    auth_headers=lambda: {},
)


def build_app(hass: FakeHass) -> web.Application:
    app = web.Application()
    base = "/api/blink_liveview_proxy/cameras/{slug}"
    playlist = views.BlinkLiveviewProxyHlsPlaylistView(hass)
    segment = views.BlinkLiveviewProxyHlsSegmentView(hass)
    mpegts = views.BlinkLiveviewProxyMpegtsView(hass)
    stop = views.BlinkLiveviewProxyStopView(hass)
    diagnostics = views.BlinkLiveviewProxyDiagnosticsView(hass)
    app.router.add_get(f"{base}/hls/index.m3u8", lambda r: playlist.get(r, r.match_info["slug"]))
    app.router.add_get(
        f"{base}/hls/{{filename}}",
        lambda r: segment.get(r, r.match_info["slug"], r.match_info["filename"]),
    )
    app.router.add_get(f"{base}/mpegts", lambda r: mpegts.get(r, r.match_info["slug"]))
    app.router.add_post(f"{base}/stop", lambda r: stop.post(r, r.match_info["slug"]))
    app.router.add_post(f"{base}/diagnostics", lambda r: diagnostics.post(r, r.match_info["slug"]))
    return app


def url(path: str, token: str = CAMERA_TOKEN, session: str = SESSION, slug: str = SLUG) -> str:
    return f"/api/blink_liveview_proxy/cameras/{slug}/{path}?token={token}&session={session}"


async def diagnose(client: TestClient, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    response = await client.post(url("diagnostics", **kwargs), data=json.dumps(payload))
    assert response.status == 200, await response.text()
    return await response.json()


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15"


async def scenario_issue_91() -> None:
    print("\nthe #91 shape: playlist served twice, then silence")
    ADDON.clear()
    ADDON["index.m3u8"] = PLAYLIST.encode()
    hass = FakeHass()
    async with TestClient(TestServer(build_app(hass))) as client:
        first = await client.get(url("hls/index.m3u8"))
        body = await first.text()
        await client.get(url("hls/index.m3u8"))
        await client.post(url("stop"))
        check(
            f"segment_00000.ts?token={CAMERA_TOKEN}&session={SESSION}" in body,
            "segment URIs carry the session, so their requests join the same trace",
            body,
        )
        result = await diagnose(client, {
            "code": "E-005", "transport": "hls", "protocol": "http:",
            "host": "homeassistant.local:8123", "ua": IPHONE,
            "events": [
                {"t": 0.0, "e": "path", "ns": 0, "rs": 0, "buf": 0, "paused": True, "d": "native HLS"},
                {"t": 0.1, "e": "loadstart", "ns": 2, "rs": 0, "buf": 0, "paused": True},
                {"t": 30.0, "e": "E-005", "ns": 2, "rs": 0, "buf": 0, "paused": True},
            ],
        })
    report = result["report"]
    check("stopped asking" in result["verdict"], "the verdict says the device stopped asking",
          result["verdict"])
    check("playlist 200" in report and "1 segment)" in report.split("playlist 200", 1)[1][:60],
          "the report shows the playlist and how many segments it listed", report)
    check(report.count("playlist 200") == 2, "both playlist requests are there", report)
    check("stop 200" in report, "and the player's stop", report)
    check("segment " not in report.split("Home Assistant received:")[1].split("The player saw:")[0],
          "and no segment request, because none came", report)
    check(CAMERA_TOKEN not in report, "the access token never enters the report")
    check("homeassistant.local" not in report and "Page: http, .local name" in report,
          "the address is reduced to its kind", report)
    check("network=LOADING ready=NOTHING buffered=0" in report,
          "the player's states are spelled out", report)
    check("iPhone OS 18_5" in report, "the device is identified, iOS version included", report)
    check(report.startswith("```") and report.endswith("```"),
          "it pastes into GitHub as a code block")


async def scenario_refused_playlist() -> None:
    print("\na refused playlist is recorded, though the add-on never saw it")
    ADDON.clear()
    ADDON["index.m3u8"] = PLAYLIST.encode()
    hass = FakeHass()
    async with TestClient(TestServer(build_app(hass))) as client:
        refused = await client.get(url("hls/index.m3u8", token="stale-token"))
        check(refused.status == 403, "the bad token is refused")
        result = await diagnose(client, {"code": "E-007", "detail": "media error 4",
                                         "transport": "hls"})
    check("refused the playlist (HTTP 403)" in result["verdict"],
          "the verdict names the refusal and its status", result["verdict"])
    check("stale-token" not in result["report"], "the refused token is not echoed either")


async def scenario_segments_delivered() -> None:
    print("\nsegments delivered, nothing buffered")
    ADDON.clear()
    ADDON["index.m3u8"] = PLAYLIST.encode()
    ADDON["segment_00000.ts"] = b"\x47" * 188 * 10
    hass = FakeHass()
    async with TestClient(TestServer(build_app(hass))) as client:
        body = await (await client.get(url("hls/index.m3u8"))).text()
        segment_uri = next(line for line in body.splitlines() if line.startswith("segment_"))
        got = await client.get(f"/api/blink_liveview_proxy/cameras/{SLUG}/hls/{segment_uri}")
        check(got.status == 200 and len(await got.read()) == 1880, "the segment is served")
        result = await diagnose(client, {"code": "E-005", "transport": "hls"})
        buffered = await diagnose(client, {"code": "E-006", "transport": "hls", "buffered": True})
    check("segment segment_00000.ts 200" in result["report"]
          and "1880 B" in result["report"],
          "the segment request is recorded with its size", result["report"])
    check("nothing buffered" in result["verdict"], "the verdict separates delivered from buffered",
          result["verdict"])
    check("never started playing" in buffered["verdict"],
          "buffered but not playing is a start-up problem", buffered["verdict"])


async def scenario_mpegts_not_ready() -> None:
    print("\nMPEG-TS refused because the add-on is not ready")
    ADDON.clear()

    def not_ready() -> None:
        raise web.HTTPServiceUnavailable(text="Blink client is signing in\n")

    ADDON["mpegts"] = not_ready
    hass = FakeHass()
    async with TestClient(TestServer(build_app(hass))) as client:
        response = await client.get(url("mpegts"))
        check(response.status == 503, "the stream request fails with the add-on's 503")
        result = await diagnose(client, {"code": "E-004",
                                         "detail": "NetworkError: HttpStatusCodeInvalid 503",
                                         "transport": "mse"})
    check("refused the stream (HTTP 503)" in result["verdict"]
          and "not ready" in result["verdict"], "the verdict explains a 503", result["verdict"])
    check("mpegts 503" in result["report"] and "signing in" in result["report"],
          "the report keeps the add-on's reason", result["report"])


async def scenario_boundaries() -> None:
    print("\nboundaries")
    ADDON.clear()
    ADDON["index.m3u8"] = PLAYLIST.encode()
    hass = FakeHass()
    async with TestClient(TestServer(build_app(hass))) as client:
        await client.get(url("hls/index.m3u8"))
        other = await diagnose(client, {"code": "E-005", "transport": "hls"}, slug="garage")
        check("No playlist request reached" in other["verdict"],
              "one camera's session is not reported under another", other["verdict"])

        await client.get(url("hls/index.m3u8", session="bad session!"))
        check(views._trace(hass).get("bad session!") is None,
              "a malformed session id is not recorded")

        forbidden = await client.post(url("diagnostics", token="nope"), data="{}")
        check(forbidden.status == 403, "diagnostics need the same token as the stream")

        huge = await client.post(url("diagnostics"), data="x" * (300 * 1024))
        check(huge.status == 413, "an oversized body is refused")

        junk = await client.post(url("diagnostics"), data="not json")
        check(junk.status == 200, "a body that is not JSON still gets the server's half")

        logged: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                logged.append(record.getMessage())

        capture = Capture()
        views.LOGGER.addHandler(capture)
        try:
            hostile = await diagnose(client, {
                "code": "E-005\n\nVerdict: forged", "transport": "hls", "played": True,
                "ua": "x\u0000y\nz", "events": [{"e": "a\nb", "t": "x"}],
            })
        finally:
            views.LOGGER.removeHandler(capture)
        lines = hostile["report"].splitlines()
        check(sum(line.startswith("Verdict:") for line in lines) == 1
              and "forged" not in hostile["report"],
              "text from the browser cannot add lines to the report", hostile["report"])
        check(logged and all("\n" not in line and "forged" not in line for line in logged),
              "nor to the Home Assistant log", logged)

    trace = trace_mod.LiveviewTrace()
    for index in range(trace_mod.MAX_SESSIONS + 5):
        trace.begin(f"session-{index:04d}", SLUG, "playlist")
    check(trace.get("session-0000") is None and trace.get(
        f"session-{trace_mod.MAX_SESSIONS + 4:04d}") is not None,
        "only the most recent sessions are kept")


def unit_verdicts() -> None:
    print("\nverdicts")
    verdict = trace_mod.verdict
    check("could not reach" in verdict(None, {"transport": "hls"}),
          "nothing received: the device could not reach Home Assistant")
    check("could not reach" in verdict(None, {"transport": "mse"}), "the same on MPEG-TS")
    check(verdict(None, {"code": "E-001b"}).startswith("This browser can play neither"),
          "E-001b needs no server half")
    check("tap" in verdict(None, {"code": "E-008"}), "E-008 is about the tap")
    check(verdict(None, {"played": True, "code": "E-007"}).startswith("The live view played"),
          "a view that played says so first")
    server = {"requests": [
        {"kind": "mpegts", "status": 200, "bytes": 0, "at": 0, "name": ""},
    ]}
    check("sent no video" in verdict(server, {"transport": "mse"}),
          "an accepted stream with no bytes points at the add-on")
    reloads = {"requests": [{"kind": "playlist", "status": 200, "at": i, "name": ""}
                            for i in range(5)]}
    check("kept reloading" in verdict(reloads, {"transport": "hls"}),
          "reloads with no segment are told apart from silence")
    check(trace_mod.playlist_segment_count(PLAYLIST) == 1, "segments are counted")
    many = {"requests": [{"kind": "playlist", "status": 200, "at": float(i), "name": "",
                          "ms": i % 2, "detail": "1 segment"} for i in range(30)]}
    lines = trace_mod._server_lines(many)
    check(len(lines) == 2 and "29 more like it" in lines[1] and "+29.0s" in lines[1],
          "a run of identical reloads is one line, not thirty", lines)


async def run_all() -> None:
    await scenario_issue_91()
    await scenario_refused_playlist()
    await scenario_segments_delivered()
    await scenario_mpegts_not_ready()
    await scenario_boundaries()


def main() -> int:
    unit_verdicts()
    asyncio.run(run_all())
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for failure in FAILURES:
            print(f"  {failure}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
