"""Tests for the address the integration offers an add-on install.

Issue #30: the add-on was working, the config flow said `cannot_connect`. The
form pre-filled `http://homeassistant.local:8088`, a host address — and the
add-on declares `ports: 8088/tcp: null`, so nothing is published to the host
until someone maps it by hand. The one advertised single-click path was the
one path that could not work.

So three things are pinned here: Supervisor's slug and hostname rules, the
order the candidate addresses are tried in, and the add-on actually writing
the address it is reachable on. No Home Assistant, no Supervisor, no network -
supervisor.py imports nothing from Home Assistant, and the config flow's
picker is loaded out of the source by AST. Run from the repo root:

    python tests/test_addon_address.py
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components/blink_liveview_proxy"

sys.path.insert(0, str(COMPONENT))
import supervisor  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def load_picker():
    """Return config_flow's address picker, with its collaborators stubbed."""
    source = (COMPONENT / "config_flow.py").read_text()
    tree = ast.parse(source)
    wanted = ("_async_addon_base_url",)
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    namespace: dict = {"HomeAssistant": object, "ADDON_BASE_URL": HOST_URL}
    exec(
        compile(ast.Module(functions, []), str(COMPONENT / "config_flow.py"), "exec"),
        namespace,
    )
    return namespace


HOST_URL = "http://homeassistant.local:8088"
INTERNAL_URL = "http://a1b2c3d4-blink-liveview-proxy:8088"
SHARED_URL = "http://a1b2c3d4-blink-liveview-proxy:9000"


def pick(*, shared: str, derived: str, answers: set[str]) -> tuple[str, list[str]]:
    """Run the picker against one arrangement of the world."""
    namespace = load_picker()
    tried: list[str] = []

    async def _async_handoff_url(hass):
        return shared

    def _supervisor_addon_url(hass):
        return derived

    async def _async_answers(hass, base_url, token):
        tried.append(base_url)
        return base_url in answers

    namespace["_async_handoff_url"] = _async_handoff_url
    namespace["_supervisor_addon_url"] = _supervisor_addon_url
    namespace["_async_answers"] = _async_answers
    chosen = asyncio.run(namespace["_async_addon_base_url"](object(), "token"))
    return chosen, tried


def test_supervisor_rules() -> None:
    print("\nSupervisor's slug and hostname rules")

    domain = "blink_liveview_proxy"
    check(
        supervisor.addon_slug({"local_blink_liveview_proxy": {}}, domain)
        == "local_blink_liveview_proxy",
        "a repository-prefixed add-on slug is found",
    )
    check(
        supervisor.addon_slug({"a1b2c3d4_blink_liveview_proxy": {}}, domain)
        == "a1b2c3d4_blink_liveview_proxy",
        "a hashed repository prefix is found too",
    )
    check(
        supervisor.addon_slug({"blink_liveview_proxy": {}}, domain)
        == "blink_liveview_proxy",
        "an unprefixed slug is found",
    )
    check(
        supervisor.addon_slug({"unrelated": {}}, domain) is None,
        "an unrelated add-on is never selected",
    )
    check(
        supervisor.addon_slug({"other_liveview_proxy": {}}, domain) is None,
        "a slug that merely ends in part of the domain is not ours",
    )
    check(
        supervisor.addon_slug(None, domain) is None,
        "no Supervisor inventory is not a crash",
    )

    check(
        supervisor.addon_internal_url("a1b2c3d4_blink_liveview_proxy") == INTERNAL_URL,
        "the hostname is the slug with underscores turned into dashes",
    )
    check(
        supervisor.addon_internal_url("local_blink_liveview_proxy", 9000)
        == "http://local-blink-liveview-proxy:9000",
        "a non-default port is carried through",
    )
    check(
        supervisor.addon_internal_url(None) == "" and supervisor.addon_internal_url("") == "",
        "no slug yields no address, never a URL with a hole in it",
    )
    check(
        "homeassistant" not in supervisor.addon_internal_url("x_blink_liveview_proxy"),
        "the internal address never falls back to a host name",
    )


def test_candidate_order() -> None:
    print("\nwhich address the add-on install is offered")

    chosen, tried = pick(
        shared=SHARED_URL, derived=INTERNAL_URL, answers={SHARED_URL, INTERNAL_URL}
    )
    check(
        chosen == SHARED_URL and tried == [SHARED_URL],
        "what the add-on itself says wins, and stops the search",
    )

    chosen, tried = pick(shared="", derived=INTERNAL_URL, answers={INTERNAL_URL, HOST_URL})
    check(
        chosen == INTERNAL_URL,
        "an older add-on that says nothing still gets the Supervisor hostname",
    )

    chosen, tried = pick(shared=SHARED_URL, derived=INTERNAL_URL, answers={INTERNAL_URL})
    check(
        chosen == INTERNAL_URL and tried == [SHARED_URL, INTERNAL_URL],
        "an address that does not answer is passed over for one that does",
    )

    chosen, tried = pick(shared="", derived="", answers={HOST_URL})
    check(
        chosen == HOST_URL and tried == [HOST_URL],
        "with no Supervisor to ask, the host address is still tried",
    )

    chosen, tried = pick(shared="", derived=INTERNAL_URL, answers=set())
    check(
        tried == [INTERNAL_URL, HOST_URL],
        "every candidate is tried before giving up",
    )
    check(
        chosen == INTERNAL_URL,
        "when nothing answers, the best guess is offered - not the known-bad one",
    )

    chosen, tried = pick(shared=INTERNAL_URL, derived=INTERNAL_URL, answers=set())
    check(
        tried == [INTERNAL_URL, HOST_URL],
        "a repeated candidate is probed once, not twice",
    )

    source = (COMPONENT / "config_flow.py").read_text()
    picker = source[source.index("async def _async_addon_base_url") :]
    body = picker[: picker.index("\nasync def ", 1) if "\nasync def " in picker[1:] else len(picker)]
    check(
        body.index("_async_handoff_url")
        < body.index("_supervisor_addon_url")
        < body.index("ADDON_BASE_URL"),
        "the host address is the last resort in the source, not the first",
    )


def test_addon_publishes_its_address() -> None:
    print("\nthe add-on says where it can be reached")

    run = (ROOT / "addon/run.sh").read_text()
    const = (COMPONENT / "const.py").read_text()

    name = re.search(r'URL_HANDOFF_FILE = "([^"]+)"', const)
    check(name is not None, "the integration names a file to read the address from")
    check(
        name is not None and f"$HA_CONFIG/{name.group(1)}" in run,
        "the add-on writes the file the integration reads",
    )
    check(
        "bashio::addon.hostname" in run and "hostname 2>/dev/null" in run,
        "the address comes from Supervisor, with the container's own name as backup",
    )
    check(
        run.index('PORT="$(bashio::config') < run.index("blink_liveview_proxy.url"),
        "the published address carries the port this start is listening on",
    )
    executable = "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )
    check(
        "homeassistant.local" not in executable,
        "the add-on never hands out an address that needs a published port",
    )

    config = (ROOT / "addon/config.yaml").read_text()
    check(
        re.search(r"^\s+8088/tcp: null", config, re.MULTILINE) is not None,
        "the add-on still publishes no host port - the reason all of this exists",
    )


def main() -> int:
    print("the address an add-on install is given")
    test_supervisor_rules()
    test_candidate_order()
    test_addon_publishes_its_address()

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
