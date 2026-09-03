"""Tests for the proxy updating itself when Home Assistant asks it to.

Two halves meet here. The proxy decides whether this install can be updated at
all and, when it can, starts the unit that does it; the integration decides
whether to put a button in front of anyone. The security-shaped property is in
the middle: the request that triggers an update carries no argument, so there
is nothing in it that could choose what gets installed on the camera host.

No Home Assistant, no systemd, no network. version_check.py imports nothing
from Home Assistant, so it is imported directly; selfupdate's systemctl calls
are replaced with a recorder. Run from the repo root:

    python tests/test_self_update.py
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import json
import pathlib
import re
import shutil
import sys
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "custom_components/blink_liveview_proxy"))

import version_check
from proxy.blink_proxy import routes, selfupdate

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


class Install:
    """Stand in for one kind of install, restoring the module afterwards."""

    def __init__(self, *, supervisor=False, systemd=False, container=False):
        self.supervisor = supervisor
        self.systemd = systemd
        self.container = container

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._saved = (
            selfupdate.UPDATE_UNIT_FILE,
            selfupdate.UPDATE_SCRIPT,
            selfupdate.DOCKER_MARKER,
            shutil.which,
        )
        self._env = selfupdate.os.environ.get("SUPERVISOR_TOKEN")

        unit, script, docker = root / "unit", root / "script", root / "dockerenv"
        if self.systemd:
            unit.write_text("[Unit]\n")
            script.write_text("#!/bin/sh\n")
        if self.container:
            docker.write_text("")
        if self.supervisor:
            selfupdate.os.environ["SUPERVISOR_TOKEN"] = "supervisor-secret"
        else:
            selfupdate.os.environ.pop("SUPERVISOR_TOKEN", None)

        selfupdate.UPDATE_UNIT_FILE = unit
        selfupdate.UPDATE_SCRIPT = script
        selfupdate.DOCKER_MARKER = docker
        shutil.which = lambda name: "/bin/systemctl" if name == "systemctl" else None
        selfupdate.detect_method.cache_clear()
        return self

    def __exit__(self, *exc):
        (
            selfupdate.UPDATE_UNIT_FILE,
            selfupdate.UPDATE_SCRIPT,
            selfupdate.DOCKER_MARKER,
            shutil.which,
        ) = self._saved
        if self._env is None:
            selfupdate.os.environ.pop("SUPERVISOR_TOKEN", None)
        else:
            selfupdate.os.environ["SUPERVISOR_TOKEN"] = self._env
        selfupdate.detect_method.cache_clear()
        self._tmp.cleanup()
        return False


class Systemctl:
    """Record systemctl invocations instead of making them."""

    def __init__(self, *, running=False, start_code=0):
        self.calls: list[tuple[str, ...]] = []
        self.running = running
        self.start_code = start_code

    async def __call__(self, *args: str) -> int:
        self.calls.append(args)
        if args[:1] == ("is-active",):
            return 0 if self.running else 3
        return self.start_code


def with_systemctl(fake):
    saved = selfupdate._systemctl
    selfupdate._systemctl = fake
    return saved


def test_detection() -> None:
    print("\nwhat kind of install this is")

    with Install(systemd=True):
        check(selfupdate.detect_method() == "systemd", "a host with the unit is systemd")
        check(selfupdate.describe()["supported"], "and it is offered an update")
        check(
            "reason" not in selfupdate.describe(),
            "a supported install explains nothing, because there is nothing to explain",
        )

    with Install(container=True):
        check(selfupdate.detect_method() == "container", "a container is a container")
        check(not selfupdate.describe()["supported"], "and cannot replace its own image")

    # The add-on is a container too. Supervisor is the more useful of the two
    # true answers, so it has to win.
    with Install(supervisor=True, container=True):
        check(selfupdate.detect_method() == "supervisor", "the add-on reports Supervisor, not container")
        check(
            not selfupdate.describe()["supported"],
            "the add-on does not update itself - Home Assistant asks Supervisor to",
        )

    with Install():
        check(selfupdate.detect_method() == "manual", "a host with no updater unit is manual")
        check(
            "install-proxy.sh" in selfupdate.describe()["reason"],
            "and is told what to run to get one",
        )

    with Install(systemd=True):
        selfupdate.detect_method()
        selfupdate.UPDATE_UNIT_FILE.unlink()
        check(
            selfupdate.detect_method() == "systemd",
            "the answer is cached: /status polls do not stat the disk every 30 seconds",
        )


async def test_start() -> None:
    print("\nstarting the updater")

    check(
        not inspect.signature(selfupdate.start).parameters,
        "start() takes no arguments, so no caller can choose what gets installed",
    )

    with Install(container=True):
        fake = Systemctl()
        saved = with_systemctl(fake)
        try:
            try:
                await selfupdate.start()
                check(False, "a container refuses to update itself")
            except selfupdate.UpdateUnavailableError:
                check(True, "a container refuses to update itself")
            check(not fake.calls, "and runs no systemctl at all")
        finally:
            selfupdate._systemctl = saved

    with Install(systemd=True):
        fake = Systemctl(running=True)
        saved = with_systemctl(fake)
        try:
            try:
                await selfupdate.start()
                check(False, "a second update is refused while one runs")
            except selfupdate.UpdateBusyError:
                check(True, "a second update is refused while one runs")
            check(
                [c for c in fake.calls if c[0] == "start"] == [],
                "and nothing is started on top of the running one",
            )
        finally:
            selfupdate._systemctl = saved

    with Install(systemd=True):
        fake = Systemctl()
        saved = with_systemctl(fake)
        try:
            result = await selfupdate.start()
            check(result["started"] is True, "an idle systemd install starts the updater")
            check(
                ("start", "--no-block", selfupdate.UPDATE_UNIT) in fake.calls,
                "--no-block, so the restart it causes cannot kill it mid-update",
            )
        finally:
            selfupdate._systemctl = saved

    with Install(systemd=True):
        fake = Systemctl(start_code=5)
        saved = with_systemctl(fake)
        try:
            try:
                await selfupdate.start()
                check(False, "a systemctl failure is reported, not swallowed")
            except selfupdate.UpdateUnavailableError as err:
                check("journalctl" in str(err), "a systemctl failure is reported, not swallowed")
        finally:
            selfupdate._systemctl = saved


def build_app(token: str | None = "proxy-secret") -> web.Application:
    app = web.Application(client_max_size=4096)
    if token:
        app["proxy_token"] = token
    app["auth_controller"] = type("C", (), {"state": "idle", "status": lambda self: {}})()
    app["client"] = None
    app["config"] = {"cameras": {}}
    app.router.add_post("/update", routes.update_handler)
    app.router.add_get("/status", routes.status_handler)
    return app


async def test_route() -> None:
    print("\nthe /update route")

    with Install(systemd=True):
        fake = Systemctl()
        saved = with_systemctl(fake)
        client = TestClient(TestServer(build_app()))
        await client.start_server()
        try:
            check((await client.post("/update")).status == 401, "no token is rejected")
            check(
                (await client.post("/update?token=proxy-secret")).status == 401,
                "a query token is rejected: this URL must not work from a pasted link",
            )
            check(
                (await client.post("/update", headers={"Authorization": "Bearer wrong"})).status == 401,
                "a wrong token is rejected",
            )

            # The body is the part that must not matter. A caller naming a
            # branch, a tag or a repository gets exactly the same update as one
            # sending nothing at all.
            response = await client.post(
                "/update",
                headers={"Authorization": "Bearer proxy-secret"},
                json={"ref": "attacker/main", "repo": "https://example.invalid/evil"},
            )
            body = await response.json()
            check(response.status == 202, "an authorized POST starts the update")
            check(body["unit"] == selfupdate.UPDATE_UNIT, "and names the unit it started")
            started = [c for c in fake.calls if c[0] == "start"]
            check(
                started == [("start", "--no-block", selfupdate.UPDATE_UNIT)],
                "the request body chooses nothing: same call, whatever was sent",
            )
        finally:
            await client.close()
            selfupdate._systemctl = saved

    with Install(systemd=True):
        fake = Systemctl(running=True)
        saved = with_systemctl(fake)
        client = TestClient(TestServer(build_app()))
        await client.start_server()
        try:
            response = await client.post("/update", headers={"Authorization": "Bearer proxy-secret"})
            check(response.status == 409, "an update already running answers 409, not an error")
        finally:
            await client.close()
            selfupdate._systemctl = saved

    with Install(container=True):
        client = TestClient(TestServer(build_app()))
        await client.start_server()
        try:
            response = await client.post("/update", headers={"Authorization": "Bearer proxy-secret"})
            check(response.status == 501, "an install that cannot update itself answers 501")
        finally:
            await client.close()

    with Install(systemd=True):
        client = TestClient(TestServer(build_app(token=None)))
        await client.start_server()
        try:
            response = await client.post("/update")
            check(response.status == 503, "a proxy with no token configured refuses to be updated")
        finally:
            await client.close()


async def test_status_privacy() -> None:
    print("\nwhat /status says about updating")

    with Install(systemd=True):
        client = TestClient(TestServer(build_app()))
        await client.start_server()
        try:
            public = await (await client.get("/status")).json()
            private = await (
                await client.get("/status", headers={"Authorization": "Bearer proxy-secret"})
            ).json()
            check(
                "update" not in public,
                "how this host installs software is not told to a stranger on the LAN",
            )
            check(private["update"]["method"] == "systemd", "an authorized caller learns the method")
        finally:
            await client.close()

    with Install(systemd=True):
        client = TestClient(TestServer(build_app(token=None)))
        await client.start_server()
        try:
            tokenless = await (await client.get("/status")).json()
            check(
                "update" not in tokenless,
                "a tokenless proxy never advertises a Fix button it must refuse",
            )
        finally:
            await client.close()


def test_integration_decision() -> None:
    print("\nwhat the integration does with that")

    systemd = {"version": "0.4.0", "update": {"method": "systemd", "supported": True}}
    addon = {
        "version": "0.4.0",
        "update": {"method": "supervisor", "supported": False, "reason": "Supervisor updates this add-on."},
    }
    container = {
        "version": "0.4.0",
        "update": {"method": "container", "supported": False, "reason": "A container cannot."},
    }
    old = {"version": "0.4.0"}

    check(version_check.is_behind("0.4.0", "0.5.1"), "an older proxy is behind this integration")
    check(not version_check.is_behind("0.5.1", "0.5.1"), "a matching proxy is not behind")
    check(not version_check.is_behind("0.5", "0.5.0"), "omitted trailing zeroes compare equally")
    check(not version_check.is_behind("0.6.0", "0.5.1"), "a newer proxy is never called behind")
    check(
        not version_check.is_behind(None, "0.5.1") and not version_check.is_behind("0.4.0", None),
        "an unreadable version on either side is not a guess to act on",
    )

    check(version_check.can_start_update(systemd), "a systemd proxy can be updated from here")
    check(version_check.can_start_update(addon), "so can the add-on, by way of Supervisor")
    check(not version_check.can_start_update(container), "a container cannot")
    check(not version_check.can_start_update(old), "and neither can a proxy too old to say")
    check(version_check.update_method(addon) == "supervisor", "the add-on is recognised by method")
    check(
        version_check.update_blocker(container) == "A container cannot.",
        "the proxy's own reason is what gets logged",
    )

    # The floor is what says something is actually broken; everything above it
    # is only worth mentioning where it can be acted on.
    check(
        version_check.review("0.2.0", "0.5.1", "0.3.0", container) == version_check.NOTICE_OUTDATED,
        "below the floor is always said, button or no button",
    )
    check(
        version_check.review("0.4.0", "0.5.1", "0.3.0", systemd) == version_check.NOTICE_BEHIND,
        "merely behind, with a way to fix it, is offered",
    )
    check(
        version_check.review("0.4.0", "0.5.1", "0.3.0", container) is None,
        "merely behind, with no way to fix it, stays quiet",
    )
    check(
        version_check.review("0.5.1", "0.5.1", "0.3.0", systemd) is None,
        "a matching proxy is never nagged",
    )


def test_strings_and_flow() -> None:
    print("\nthe repair flow can say everything it needs to")

    repairs = (ROOT / "custom_components/blink_liveview_proxy/repairs.py").read_text()
    updates = (ROOT / "custom_components/blink_liveview_proxy/updates.py").read_text()
    strings = json.loads((ROOT / "custom_components/blink_liveview_proxy/strings.json").read_text())
    issues = strings["issues"]

    check("async_create_fix_flow" in repairs, "the integration offers a repair flow")
    reasons = set(re.findall(r'^ABORT_[A-Z_]+ = "([a-z_]+)"', updates, re.MULTILINE))
    check(bool(reasons), "the flow names its abort reasons")

    fixable_keys = ("proxy_outdated_fixable", "proxy_behind")
    for key in fixable_keys:
        flow = issues.get(key, {}).get("fix_flow", {})
        check(bool(flow.get("step", {}).get("confirm")), f"{key} has a confirm step to show")
        missing = reasons - set(flow.get("abort", {}))
        check(not missing, f"{key} can explain every abort ({', '.join(sorted(missing)) or 'none missing'})")

    # A placeholder with nothing to fill it renders as a literal brace to the
    # user, so the coordinator has to supply every name the strings use.
    coordinator = (ROOT / "custom_components/blink_liveview_proxy/coordinator.py").read_text()
    block = coordinator.split("translation_placeholders")[1].split("\n            },")[0]
    supplied = set(re.findall(r'"(\w+)": ', block))
    for key, issue in issues.items():
        check(
            ("description" in issue) != ("fix_flow" in issue),
            f"{key} defines exactly one of description and fix_flow",
        )
        serialized = json.dumps(issue)
        used = set(re.findall(r"\{(\w+)\}", serialized))
        check(
            used <= supplied,
            f"{key}'s description only uses placeholders that are passed "
            f"({', '.join(sorted(used - supplied)) or 'all present'})",
        )

    check(
        "data={\"entry_id\": entry.entry_id}" in coordinator
        or 'data={"entry_id": entry.entry_id}' in coordinator,
        "the issue carries the entry the flow has to act on",
    )

    check(
        "NOTICE_OUTDATED_FIXABLE if outdated and fixable else notice" in coordinator,
        "an actionable old proxy uses repair-flow strings",
    )

    updates_path = ROOT / "custom_components/blink_liveview_proxy/updates.py"
    updates = updates_path.read_text()
    check(
        "get_addons_info" in updates and "get_supervisor_info" not in updates,
        "the add-on is found in Supervisor's add-on inventory",
    )
    check(
        "hassio.update_helper" in updates and "async_update_addon" in updates,
        "current and minimum-supported Home Assistant can both update the add-on",
    )

    tree = ast.parse(updates)
    slug_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_addon_slug"
    )
    namespace = {"DOMAIN": "blink_liveview_proxy"}
    exec(compile(ast.Module([slug_function], []), str(updates_path), "exec"), namespace)
    addon_slug = namespace["_addon_slug"]
    check(
        addon_slug({"local_blink_liveview_proxy": {}})
        == "local_blink_liveview_proxy",
        "a repository-prefixed add-on slug is found",
    )
    check(
        addon_slug({"unrelated": {}}) is None,
        "an unrelated add-on is never selected",
    )


def test_installer() -> None:
    print("\nthe installer puts the updater on every host")

    script = (ROOT / "scripts/install-proxy.sh").read_text()
    install_line = script.index("/usr/local/sbin/blink-liveview-proxy-update.sh")
    guard = script.index('if [ "${INSTALL_AUTOUPDATE:-0}" = "1" ]')
    enable = script.index("systemctl enable --now blink-liveview-proxy-update.timer")

    check(
        install_line < guard,
        "the updater is installed unconditionally, so the button exists without the timer",
    )
    check(enable > guard, "but the daily schedule stays behind INSTALL_AUTOUPDATE")
    check(
        "update.env" in script,
        "and the updater is still told which checkout this install came from",
    )


async def async_main() -> None:
    test_detection()
    await test_start()
    await test_route()
    await test_status_privacy()
    test_integration_decision()
    test_strings_and_flow()
    test_installer()


def main() -> int:
    asyncio.run(async_main())
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
