"""The Blink device poll, and the routes Home Assistant's Blink entities use.

The rule this pins above everything else: a failed refresh must never become
a sign-in. The official integration's blinkpy fell back to a password login
when its refresh token died, Blink texted a 2FA code for every attempt, and
the account was locked out for hours. So the first checks here run blinkpy's
real Auth and Blink classes, fake only the network edge, and record every
entry point that could start a sign-in or a 2FA challenge. None may be
reached, the refresh-token grant is the only thing sent, and the poll stops
asking instead of retrying.

No Home Assistant, no Blink account, no network. Run from the repo root:

    python tests/test_device_poll.py
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import sys
import tempfile
import time
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blinkpy import api as blink_api  # noqa: E402
from blinkpy.auth import Auth  # noqa: E402
from blinkpy.blinkpy import Blink  # noqa: E402

from blink_proxy import devices  # noqa: E402
from blink_proxy.blink import BlinkClient  # noqa: E402
from blink_proxy.devices import (  # noqa: E402
    BACKOFF_MAX_SECONDS,
    DevicePoller,
    backoff_seconds,
    clamp_interval,
)
sys.path.insert(0, str(ROOT / "tests"))
from blink_fakes import FakeBlink  # noqa: E402
from blink_proxy.routes import (  # noqa: E402
    devices_handler,
    motion_detection_handler,
    snapshot_handler,
    snapshot_refresh_handler,
    status_handler,
    sync_arm_handler,
)

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def make_poller(client: Any, clock: FakeClock) -> DevicePoller:
    return DevicePoller(
        lambda: client, clock=clock, wall=clock, sleep=clock.sleep
    )


# ---------------------------------------------------------------------------
# Real blinkpy, fake network


class FakeLoginResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def json(self) -> dict[str, Any]:
        return {}


class SignInTrap:
    """Records every blinkpy entry point that could start a sign-in."""

    NAMES = (
        ("auth", "startup"),
        ("auth", "_oauth_login_flow"),
        ("auth", "complete_2fa_login"),
        ("blink", "start"),
        ("blink", "send_2fa_code"),
        ("api", "oauth_authorize_request"),
        ("api", "oauth_get_signin_page"),
        ("api", "oauth_signin"),
        ("api", "oauth_verify_2fa"),
        ("api", "oauth_exchange_code_for_token"),
    )

    def __init__(self) -> None:
        self.sign_ins: list[str] = []
        self.logins: list[dict[str, Any]] = []
        self._saved: list[tuple[Any, str, Any]] = []

    def install(self, login_status: int) -> None:
        targets = {"auth": Auth, "blink": Blink, "api": blink_api}
        for owner_name, attr in self.NAMES:
            owner = targets[owner_name]
            self._saved.append((owner, attr, getattr(owner, attr)))

            async def trapped(*_args, _label=f"{owner_name}.{attr}", **_kwargs):
                self.sign_ins.append(_label)
                raise AssertionError(f"{_label} must never run from the poll")

            setattr(owner, attr, trapped)

        async def request_login(auth, url, login_data, is_refresh=False, is_retry=False):
            self.logins.append(
                {
                    "is_refresh": is_refresh,
                    "password_sent": (not is_refresh) and bool(login_data.get("password")),
                }
            )
            return FakeLoginResponse(login_status)

        self._saved.append((blink_api, "request_login", blink_api.request_login))
        blink_api.request_login = request_login

    def restore(self) -> None:
        for owner, attr, value in reversed(self._saved):
            setattr(owner, attr, value)
        self._saved.clear()


def real_blink(with_password: bool) -> Blink:
    """A blinkpy session whose access token has expired, as after a long idle."""
    login_data = {
        "username": "someone@example.com",
        "token": "expired-access-token",
        "refresh_token": "refresh-token",
        "expiration_date": time.time() - 3600,
        "expires_in": 3600,
        "hardware_id": "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
        "host": "rest-u011.immedia-semi.com",
        "region_id": "u011",
        "account_id": 1,
        "client_id": 1,
        "user_id": 1,
    }
    if with_password:
        # The proxy never stores one, but an env file might still supply it.
        login_data["password"] = "not-to-be-sent"
    auth = Auth(login_data=login_data, no_prompt=True, session=object())
    blink = Blink(refresh_rate=60, session=object())
    blink.auth = auth
    blink.setup_urls()
    blink.available = True
    return blink


class ReadyClient:
    def __init__(self, blink: Any) -> None:
        self.blink = blink
        self.ready = True


async def test_refresh_failure_never_signs_in() -> None:
    for status, label in ((401, "a rejected refresh token (401)"),
                          (500, "a failing login endpoint (500)")):
        for with_password in (False, True):
            print(f"\n{label}, password {'present' if with_password else 'absent'}")
            trap = SignInTrap()
            trap.install(login_status=status)
            try:
                clock = FakeClock()
                client = ReadyClient(real_blink(with_password))
                poller = make_poller(client, clock)
                await poller.poll_once()

                check(trap.sign_ins == [], "no sign-in or 2FA entry point was reached")
                check(len(trap.logins) == 1 and trap.logins[0]["is_refresh"],
                      "exactly one request, and it was the refresh-token grant")
                check(not any(item["password_sent"] for item in trap.logins),
                      "no password was sent")
                check(poller.state == "auth_failed", "the poll reports auth_failed")
                check(poller.last_error is not None, "and says what failed")

                # Every later tick for the same session stays silent.
                for _ in range(5):
                    clock.now += BACKOFF_MAX_SECONDS + 1
                    await poller.poll_once()
                check(len(trap.logins) == 1,
                      "the same session is never asked again, however long it waits")
                check(trap.sign_ins == [], "still no sign-in after repeated ticks")

                # A restart or a new login replaces the client; polling resumes.
                replacement = ReadyClient(real_blink(with_password))
                poller._get_client = lambda: replacement
                await poller.poll_once()
                check(len(trap.logins) == 2,
                      "a replaced session is polled again, once")
                check(trap.sign_ins == [], "and that attempt does not sign in either")
            finally:
                trap.restore()


# ---------------------------------------------------------------------------
# Fake blinkpy (tests/blink_fakes.py), for pacing and the routes


async def test_backoff_and_pacing() -> None:
    print("\ninterval and backoff arithmetic")
    check(clamp_interval(None) == 300 and clamp_interval("junk") == 300,
          "a missing or unreadable interval is the default")
    check(clamp_interval(5) == 60 and clamp_interval(99999) == 3600,
          "intervals are clamped to 60-3600 s")
    check([backoff_seconds(300, n) for n in range(4)] == [300, 600, 1200, 2400],
          "each failure doubles the wait")
    check(backoff_seconds(300, 50) == BACKOFF_MAX_SECONDS, "and it is capped at an hour")

    print("\na failing poll backs off instead of looping")
    clock = FakeClock()
    blink = FakeBlink()
    blink.clock = clock
    blink.fail_with = RuntimeError("Blink is down")
    client = ReadyClient(blink)
    poller = make_poller(client, clock)
    poller.interval = 300
    started = clock.now
    for _ in range(4):
        await poller.poll_once()
        clock.now = poller._next_at
    gaps = [b - a for a, b in zip(blink.refreshes, blink.refreshes[1:])]
    check(gaps == [600, 1200, 2400], f"attempts spread out: {gaps}")
    check(poller.state == "backoff" and poller.failures == 4,
          "the poll reports backoff and counts the failures")
    check(blink.refreshes[0] == started, "the first attempt was not delayed")

    blink.fail_with = None
    await poller.poll_once()
    check(poller.state == "ok" and poller.failures == 0,
          "one good poll clears the backoff")
    check(poller._next_at - clock.now == 300, "and the interval is back to normal")

    print("\na refresh that updates no sync module is not fresh data")
    for sync in blink.sync.values():
        sync.available = False
    await poller.poll_once()
    check(poller.state == "backoff", "reported as a failure, not as ok")
    for sync in blink.sync.values():
        sync.available = True

    print("\nno clip is left in memory")
    for camera in blink.cameras.values():
        camera._cached_video = b"a whole clip"
    await poller.poll_once()
    check(all(camera._cached_video == b"" for camera in blink.cameras.values()),
          "every cached clip is dropped after a poll")

    print("\nnot ready means no Blink call")
    idle = FakeBlink()
    unready = ReadyClient(idle)
    unready.ready = False
    other = make_poller(unready, FakeClock())
    await other.poll_once()
    check(idle.refreshes == [] and other.state == "not_ready",
          "a client that is not ready is left alone")

    print("\nthe loop polls on the interval, and stops when nobody reads")
    clock = FakeClock()
    blink = FakeBlink()
    blink.clock = clock
    poller = make_poller(ReadyClient(blink), clock)
    poller.demand(300)
    start = clock.now
    await asyncio.wait_for(poller._task, timeout=5)
    offsets = [round(t - start) for t in blink.refreshes]
    check(offsets == [0, 300, 600, 900], f"polled every 300 s: {offsets}")
    check(poller.state == "idle", "then stopped: no reader for three intervals")

    poller.demand(120)
    check(poller.interval == 120, "a reader can set the interval")
    await poller.close()


async def test_routes() -> None:
    print("\n/devices and the action routes")
    with tempfile.TemporaryDirectory() as tmp:
        config = {
            "auth_file": str(pathlib.Path(tmp) / "auth.json"),
            "cameras": {"driveway": {"id": "909058", "serial": "SERIAL-A"}},
        }
        client = BlinkClient(config, pathlib.Path(tmp), None)
        blink = FakeBlink()
        client.blink = blink
        client.ready = True

        app = web.Application()
        app["client"] = client
        app["config"] = config
        app["proxy_token"] = "secret-token"
        app["device_poller"] = DevicePoller(lambda: app["client"])

        class Controller:
            state = "success"

        app["auth_controller"] = Controller()
        app.router.add_get("/status", status_handler)
        app.router.add_get("/devices", devices_handler)
        app.router.add_get("/cameras/{slug}/snapshot.jpg", snapshot_handler)
        app.router.add_post("/cameras/{slug}/snapshot", snapshot_refresh_handler)
        app.router.add_post("/cameras/{slug}/motion", motion_detection_handler)
        app.router.add_post("/sync/{network_id}/arm", sync_arm_handler)
        bearer = {"Authorization": "Bearer secret-token"}

        async with TestClient(TestServer(app)) as http:
            resp = await http.get("/devices")
            check(resp.status == 401, "/devices needs the token")

            resp = await http.get("/devices?poll=120", headers=bearer)
            data = await resp.json()
            check(resp.status == 200, "/devices answers with the token")
            slugs = [row["slug"] for row in data["cameras"]]
            check(slugs == ["back_door", "driveway"],
                  f"cameras keyed by the proxy's slugs: {slugs}")
            driveway = next(row for row in data["cameras"] if row["slug"] == "driveway")
            check(driveway["motion_enabled"] is True and driveway["temperature"] == 68,
                  "motion and calibrated temperature come through")
            check(driveway["battery_low"] is False and driveway["battery_voltage"] == 165,
                  "battery state and voltage come through")
            check(driveway["snapshot_url"] == "/cameras/driveway/snapshot.jpg"
                  and driveway["snapshot_id"],
                  "each camera names its snapshot and its current id")
            check(data["sync_modules"] == [{
                "name": "114 Cooper", "network_id": "2001", "sync_id": "77",
                "serial": "SYNC-SERIAL", "status": "online", "armed": True}],
                "the sync module and its armed state")
            check(data["poll"]["interval"] == 120, "the requested interval is used")
            check(app["device_poller"]._task is not None,
                  "reading /devices starts the poll")

            status = await (await http.get("/status", headers=bearer)).json()
            check("device_poll" in status, "/status reports the poll to the token holder")
            status = await (await http.get("/status")).json()
            check("device_poll" not in status, "and not to anyone else")

            resp = await http.get("/cameras/driveway/snapshot.jpg", headers=bearer)
            check(resp.status == 200 and await resp.read() == b"JPEG-1",
                  "the cached thumbnail is served")
            resp = await http.get("/cameras/nowhere/snapshot.jpg", headers=bearer)
            check(resp.status == 404, "an unknown camera is a 404")
            blink.cameras["Back Door"]._cached_image = None
            resp = await http.get("/cameras/back_door/snapshot.jpg", headers=bearer)
            check(resp.status == 404, "no cached thumbnail is a 404, not an empty image")

            resp = await http.post(
                "/cameras/driveway/motion?token=secret-token", json={"enabled": False}
            )
            check(resp.status == 401, "a device action refuses a token in the URL")
            resp = await http.post(
                "/cameras/driveway/motion", headers=bearer, json={"enabled": "no"}
            )
            check(resp.status == 400, "enabled must be a real boolean")
            resp = await http.post(
                "/cameras/driveway/motion", headers=bearer, json={"enabled": False}
            )
            row = await resp.json()
            camera = blink.cameras["Driveway"]
            check(resp.status == 200 and camera.arm_calls == [False],
                  "motion detection is switched through blinkpy")
            check(row["motion_enabled"] is False,
                  "the answer is the camera's state re-read after the change")

            before = (await (await http.get("/devices", headers=bearer)).json())
            before_id = next(r for r in before["cameras"] if r["slug"] == "driveway")["snapshot_id"]
            resp = await http.post("/cameras/driveway/snapshot", headers=bearer)
            row = await resp.json()
            check(resp.status == 200 and camera.snaps == 1, "a new picture is taken")
            check(row["snapshot_id"] != before_id, "and the snapshot id moves")
            resp = await http.get("/cameras/driveway/snapshot.jpg", headers=bearer)
            check(await resp.read() == b"JPEG-2", "the new picture is what is served")

            resp = await http.post("/sync/2001/arm", headers=bearer, json={"armed": False})
            row = await resp.json()
            check(resp.status == 200 and row["armed"] is False,
                  "the sync module is disarmed and re-read")
            resp = await http.post("/sync/9999/arm", headers=bearer, json={"armed": True})
            check(resp.status == 404, "an unknown network is a 404")

            print("\ncommands Blink answers but does not apply")
            camera.ignore_arm = True
            resp = await http.post(
                "/cameras/driveway/motion", headers=bearer, json={"enabled": True}
            )
            check(resp.status == 502 and "did not apply" in await resp.text(),
                  "a motion change Blink did not apply is an error, not a success")
            camera.ignore_arm = False

            devices.SNAPSHOT_RETRY_SECONDS = 0
            camera.thumbnail_not_ready = 2
            resp = await http.post("/cameras/driveway/snapshot", headers=bearer)
            check(resp.status == 200 and camera.snaps == 2,
                  "a thumbnail not ready at first is fetched again until it is")
            resp = await http.get("/cameras/driveway/snapshot.jpg", headers=bearer)
            check(await resp.read() == b"JPEG-3", "and the new image is served")
            camera.thumbnail_not_ready = 99
            resp = await http.post("/cameras/driveway/snapshot", headers=bearer)
            check(resp.status == 502 and "could not be downloaded" in await resp.text(),
                  "one that never arrives is an error, not the old picture again")
            camera.thumbnail_not_ready = 0
            row = await (await http.get("/devices", headers=bearer)).json()
            served = await (await http.get("/cameras/driveway/snapshot.jpg",
                                           headers=bearer)).read()
            same_id = [r for r in row["cameras"] if r["slug"] == "driveway"][0]["snapshot_id"]
            check(served == b"JPEG-3" and same_id == devices._snapshot_id(camera),
                  "and the snapshot id still names the image actually served")

            sync = blink.sync["114 Cooper"]
            original = sync.get_network_info

            async def unchanged() -> bool:
                return True

            sync.get_network_info = unchanged
            resp = await http.post("/sync/2001/arm", headers=bearer, json={"armed": True})
            check(resp.status == 502, "an arm change Blink did not apply is an error")
            sync.get_network_info = original

            async def refused(_value: bool) -> None:
                return None

            camera.async_arm = refused
            resp = await http.post(
                "/cameras/driveway/motion", headers=bearer, json={"enabled": True}
            )
            check(resp.status == 502, "a command Blink did not accept is a 502")

        await app["device_poller"].close()

    print("\ntokenless proxy")
    with tempfile.TemporaryDirectory() as tmp:
        config = {"auth_file": str(pathlib.Path(tmp) / "auth.json"), "cameras": {}}
        client = BlinkClient(config, pathlib.Path(tmp), None)
        client.blink = FakeBlink()
        client.ready = True
        app = web.Application()
        app["client"] = client
        app["proxy_token"] = ""
        app["device_poller"] = DevicePoller(lambda: app["client"])
        app.router.add_post("/cameras/{slug}/motion", motion_detection_handler)
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/cameras/driveway/motion", json={"enabled": True})
            check(resp.status == 200,
                  "without a configured token, actions work like every other route")
        await app["device_poller"].close()


async def test_module_never_calls_a_sign_in() -> None:
    print("\nsource guard")
    tree = ast.parse(pathlib.Path(devices.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # The docstring names these to explain why they are absent; no call may.
    for name in ("start", "startup", "send_2fa_code", "_oauth_login_flow",
                 "complete_2fa_login", "refresh_tokens", "login"):
        check(name not in called, f"devices.py never calls .{name}()")


async def main() -> int:
    await test_refresh_failure_never_signs_in()
    await test_backoff_and_pacing()
    await test_routes()
    await test_module_never_calls_a_sign_in()
    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} failed")
        return 1
    print(f"\nall passed ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
