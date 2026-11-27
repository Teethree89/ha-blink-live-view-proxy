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
import time
import types
from typing import Any, ClassVar

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

    storage = module("homeassistant.helpers.storage")

    class Store:
        """In memory, and saves at once: what was saved is what reloads."""

        saved: ClassVar[dict[str, Any]] = {}

        def __init__(self, _hass, _version, key) -> None:
            self.key = key

        async def async_load(self):
            return Store.saved.get(self.key)

        def async_delay_save(self, data_func, _delay) -> None:
            Store.saved[self.key] = json.loads(json.dumps(data_func()))

    storage.Store = Store


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
    panel = views.BlinkLiveviewProxyPanelDiagnosticsView(hass)
    app.router.add_get("/api/blink_liveview_proxy/panel/diagnostics", panel.get)
    app.router.add_delete("/api/blink_liveview_proxy/panel/diagnostics", panel.delete)
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
        check(
            first.headers.get("Content-Type") == "application/vnd.apple.mpegurl",
            "the playlist's type has no charset (Apple mediastreamvalidator 451001, #91)",
            first.headers.get("Content-Type"),
        )
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
        trace.verify(f"session-{index:04d}", SLUG, trace.begin(f"session-{index:04d}", SLUG, "playlist"))
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
    # The page's warm-up fetch (#91) is not the video player reloading.
    warmed = {"requests": [
        {"kind": "playlist", "status": 200, "at": 0.0, "name": "warm-up"},
        {"kind": "playlist", "status": 200, "at": 2.5, "name": ""},
        {"kind": "playlist", "status": 200, "at": 2.6, "name": ""},
    ]}
    check("stopped asking" in verdict(warmed, {"transport": "hls", "code": "E-005"}),
          "a warm-up plus two player fetches still reads as stopped asking, not reloading")
    only_warm = {"requests": [{"kind": "playlist", "status": 200, "at": 0.0, "name": "warm-up"}]}
    check("never asked for it" in verdict(only_warm, {"transport": "hls", "code": "E-005"}),
          "a warm-up with no player request says the video player never asked")
    slow_camera = {"requests": [{"kind": "playlist", "status": 504, "at": 0.0, "name": "warm-up"}]}
    check("did not start sending video in time" in verdict(slow_camera, {"transport": "hls"}),
          "a camera too slow to start is named as such, not as the add-on failing")
    refused_warm = {"requests": [{"kind": "playlist", "status": 403, "at": 0.0, "name": "warm-up"}]}
    check("refused the playlist (HTTP 403)" in verdict(refused_warm, {"transport": "hls"}),
          "a refused warm-up still names the refusal")
    # Airplane mode switched on while a live view loaded: Home Assistant served
    # the playlist and saw nothing more, the #91 shape, but the report waited
    # on the device. It must not be told it stopped asking.
    one_playlist = {"requests": [{"kind": "playlist", "status": 200, "at": 0.0, "name": ""}]}
    offline = verdict(one_playlist, {"transport": "hls", "code": "E-007", "queued": True,
                                     "unreachable": True})
    check("could not reach Home Assistant" in offline and "stopped asking" not in offline,
          "a report sent late blames the connection, not the device's player", offline)
    # Home Assistant answered the first try with an error: it was reachable,
    # so the report waited but what Home Assistant saw still decides.
    refused_stream = {"requests": [{"kind": "mpegts", "status": 503, "at": 0.0, "name": "",
                                    "bytes": 0}]}
    answered = verdict(refused_stream, {"transport": "mse", "code": "E-004", "queued": True,
                                        "unreachable": False, "answered": 503})
    check("refused the stream (HTTP 503)" in answered and "could not reach" not in answered,
          "a report that waited because Home Assistant answered with an error keeps its "
          "real verdict", answered)
    report = trace_mod.build_report(
        refused_stream, {"transport": "mse", "queued": True, "answered": 503}, slug=SLUG, version="t")
    check("Home Assistant answered (HTTP 503)" in report and "could not reach" not in report,
          "and its report says Home Assistant answered, not that it was unreachable", report)
    check("stopped asking" in verdict(one_playlist, {"transport": "hls", "code": "E-005"}),
          "while the same requests reported at once are still the #91 verdict")
    refused_stream = {"requests": [{"kind": "mpegts", "status": 503, "bytes": 0, "at": 0.0,
                                    "name": ""}]}
    summary = trace_mod.server_summary(refused_stream)
    check("refused the stream (HTTP 503)" in summary and "MB" not in summary,
          "an unreported live view that was refused says so, not '0.0 MB streamed'", summary)
    many = {"requests": [{"kind": "playlist", "status": 200, "at": float(i), "name": "",
                          "ms": i % 2, "detail": "1 segment"} for i in range(30)]}
    lines = trace_mod._server_lines(many)
    check(len(lines) == 2 and "29 more like it" in lines[1] and "+29.0s" in lines[1],
          "a run of identical reloads is one line, not thirty", lines)


async def scenario_history() -> None:
    print("\nsaved history for the admin panel")
    ADDON.clear()
    ADDON["index.m3u8"] = PLAYLIST.encode()
    sys.modules["homeassistant.helpers.storage"].Store.saved.clear()
    hass = FakeHass()
    panel = "/api/blink_liveview_proxy/panel/diagnostics"
    other = "1a2b3c4d-0000-4000-8000-000000000002"
    unreported = "1a2b3c4d-0000-4000-8000-000000000003"
    an_hour_ago = int(time.time()) - 3600
    async with TestClient(TestServer(build_app(hass))) as client:
        # A live view that failed and was reported at once.
        await client.get(url("hls/index.m3u8"))
        await diagnose(client, {"code": "E-005", "transport": "hls"})
        # The same session reporting again (a failed Save afterwards).
        await diagnose(client, {"code": "E-009", "detail": "HTTP 404", "transport": "hls"})
        # One sent late, from a device that was offline, with when it happened.
        await diagnose(client, {"code": "E-007", "transport": "hls", "queued": True,
                                "unreachable": True,
                                "started_at": an_hour_ago * 1000}, session=other)
        # One nobody reported on: playlist fetched, then the device vanished.
        await client.get(url("hls/index.m3u8", session=unreported))

        listed = await (await client.get(panel, params={"slug": SLUG})).json()
        entries = listed["entries"]
        by_session = {entry["session"]: entry for entry in entries}
        check(len(entries) == 3, "one entry per live view: reported twice is still one",
              [e["session"] for e in entries])
        check(by_session[SESSION]["code"] == "E-009" and by_session[SESSION]["source"] == "device",
              "a second report for a session replaces the first", by_session.get(SESSION))
        late = by_session[other]
        check(late["queued"] and late["at"] == an_hour_ago
              and "Sent late" in late["report"],
              "a late report is marked as late and dated when it happened", late)
        silent = by_session[unreported]
        check(silent["source"] == "server" and "No report from the device" in silent["verdict"]
              and "never closed it" in silent["verdict"],
              "a live view nobody reported on is listed from what Home Assistant saw",
              silent["verdict"])
        check(entries[-1]["session"] == other, "oldest last: entries are newest first",
              [e["at"] for e in entries])
        check(all(CAMERA_TOKEN not in entry["report"] for entry in entries),
              "no saved report holds a token")

        garage = await (await client.get(panel, params={"slug": "garage"})).json()
        check(garage["entries"] == [], "each camera lists only its own")

        saved = sys.modules["homeassistant.helpers.storage"].Store.saved
        check(len(saved.get(views.REPORT_STORE_KEY, {}).get("reports", [])) == 2,
              "reports are written to storage, so a restart keeps them")

        cleared = await (await client.delete(panel, params={"slug": SLUG})).json()
        after = await (await client.get(panel, params={"slug": SLUG})).json()
        check(cleared["cleared"] == 2 and after["entries"] == [],
              "clearing removes the saved reports and the unreported live views", after)
        missing = await client.delete(panel)
        check(missing.status == 400, "clearing needs a camera")

    fresh = FakeHass()
    log, _store = await views._report_log(fresh)
    check(log.reports == [], "after clearing, a reload finds nothing")
    for index in range(trace_mod.MAX_SAVED_REPORTS + 10):
        log.add({"session": f"s{index}", "slug": SLUG})
    check(len(log.reports) == trace_mod.MAX_SAVED_REPORTS and log.reports[-1]["session"] == "s59",
          "only the most recent reports are kept")


async def scenario_no_token_cannot_plant() -> None:
    print("\nrequests with no token cannot reach the admin panel or evict real views")
    ADDON.clear()
    ADDON["index.m3u8"] = PLAYLIST.encode()
    hass = FakeHass()
    panel = "/api/blink_liveview_proxy/panel/diagnostics"
    base = f"/api/blink_liveview_proxy/cameras/{SLUG}"
    forged = ("x%0A    +0.0s  playlist 200  (0 ms, 9 segments)%0A"
              "    +0.1s  segment%0D%0Aforged.ts")
    async with TestClient(TestServer(build_app(hass))) as client:
        # A real live view, with a token.
        await client.get(url("hls/index.m3u8"))
        # Then a flood with no token at all, on invented sessions.
        for index in range(trace_mod.MAX_SESSIONS + 10):
            response = await client.get(f"{base}/mpegts?session=flood{index:06d}")
            assert response.status == 403
        planted = await client.get(f"{base}/hls/{forged}?session=attacker0001")
        check(planted.status == 403, "the forged segment request is refused")

        trace = views._trace(hass)
        check(trace.get(SESSION) is not None,
              "the flood did not push the real live view out of the trace")
        check([session for session, _ in trace.sessions()] == [SESSION],
              "no unverified session is among those the panel can list",
              [session for session, _ in trace.sessions()])
        listed = await (await client.get(panel, params={"slug": SLUG})).json()
        check([entry["session"] for entry in listed["entries"]] == [SESSION],
              "the admin panel lists only the real live view",
              [entry["session"] for entry in listed["entries"]])

        # Even the device that owns an unverified session (it would need the
        # UUID) sees the file name on one line, so no line can be forged.
        held = trace.get("attacker0001")
        report = trace_mod.build_report(held, {"transport": "hls"}, slug=SLUG, version="t")
        received = report.split("Home Assistant received:", 1)[1].split("The player saw:", 1)[0]
        check(held is not None and len([line for line in received.splitlines() if line.strip()]) == 1
              and "\r" not in report,
              "a file name from the URL cannot add lines to a report", report)

        # A refused request on a real live view's session still lands in it.
        late = await client.get(url("hls/segment_00001.ts", token="stale-token"))
        check(late.status == 403 and any(
            record["status"] == 403 for record in trace.get(SESSION)["requests"]),
            "a refused request on a real live view is still recorded in it")

    # A flood of unverified sessions mid-request does not lose the real one.
    trace = trace_mod.LiveviewTrace()
    record = trace.begin("realsession01", SLUG, "playlist")
    for index in range(trace_mod.MAX_UNVERIFIED_SESSIONS + 5):
        trace.begin(f"flood{index:06d}", SLUG, "mpegts")
    trace.verify("realsession01", SLUG, record)
    trace.finish(record, 200)
    kept = trace.get("realsession01")
    check(kept is not None and kept["requests"] == [record] and record["status"] == 200,
          "a session pushed out while its request was being authorised is kept", kept)


async def scenario_settings() -> None:
    print("\nwarm-up setting")
    sys.modules["homeassistant.helpers.storage"].Store.saved.clear()
    hass = FakeHass()
    settings_view = views.BlinkLiveviewProxyPanelSettingsView(hass)
    app = web.Application()
    app.router.add_post("/api/blink_liveview_proxy/panel/settings", settings_view.post)
    async with TestClient(TestServer(app)) as client:
        url_settings = "/api/blink_liveview_proxy/panel/settings"
        settings, _store = await views._liveview_settings(hass)
        check(settings["hls_warm_up"] is False, "off by default")
        on = await client.post(url_settings, data=json.dumps({"hls_warm_up": True}))
        check(on.status == 200 and (await on.json())["settings"]["hls_warm_up"] is True,
              "the panel can turn it on")
        saved = sys.modules["homeassistant.helpers.storage"].Store.saved.get(views.SETTINGS_STORE_KEY)
        check(saved == {"hls_warm_up": True}, "and it is stored, so a restart keeps it", saved)
        wrong = await client.post(url_settings, data=json.dumps({"hls_warm_up": 1}))
        unknown = await client.post(url_settings, data=json.dumps({"something": True}))
        check(wrong.status == 400 and unknown.status == 400,
              "a value that is not true or false, or an unknown setting, is refused")
    fresh = FakeHass()
    reloaded, _store = await views._liveview_settings(fresh)
    check(reloaded["hls_warm_up"] is True, "a reload reads it back")

    real_snapshot_style = views._snapshot_style
    views._snapshot_style = lambda _hass, _camera: ""  # needs Home Assistant proper
    try:
        page_off = views._player_html(hass, SLUG, {"slug": SLUG}, "tok", False)
        page_on = views._player_html(hass, SLUG, {"slug": SLUG}, "tok", True)
    finally:
        views._snapshot_style = real_snapshot_style
    check("const warmUp = false;" in page_off and "const warmUp = true;" in page_on,
          "the player page is rendered with the switch's state")

    cold = {"requests": [{"kind": "playlist", "status": 200, "at": 0.0, "name": ""},
                         {"kind": "playlist", "status": 200, "at": 2.5, "name": ""}]}
    client_report = {"transport": "hls", "code": "E-005"}
    check(trace_mod.suggests_warm_up(cold, client_report)
          and "Warm up camera before starting" in trace_mod.verdict(cold, client_report),
          "the #91 shape without a warm-up suggests turning it on")
    warmed = {"requests": [{"kind": "playlist", "status": 200, "at": 0.0, "name": "warm-up"}] + cold["requests"]}
    check(not trace_mod.suggests_warm_up(warmed, client_report)
          and "Warm up camera" not in trace_mod.verdict(warmed, client_report),
          "but not when the warm-up already ran")
    check(not trace_mod.suggests_warm_up(cold, {"transport": "mse", "code": "E-002"}),
          "and never on the MPEG-TS path")


async def run_all() -> None:
    await scenario_issue_91()
    await scenario_refused_playlist()
    await scenario_segments_delivered()
    await scenario_mpegts_not_ready()
    await scenario_boundaries()
    await scenario_history()
    await scenario_settings()
    await scenario_no_token_cannot_plant()


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
