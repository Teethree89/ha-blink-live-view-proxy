"""Tests for registering the dialog module as a Lovelace resource.

No Home Assistant, no network. The function is loaded out of __init__.py by
source, because importing the package pulls in Home Assistant. Run from the
repo root:

    python tests/test_frontend_resource.py

This runs on every config-entry setup, so being idempotent matters more than
anything else here: a bug that adds the resource each time would quietly grow
the user's resource list on every restart.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "custom_components/blink_liveview_proxy/__init__.py"
URL = "/api/blink_liveview_proxy/static/blink-liveview-dialog.js"

logging.getLogger("test").setLevel(logging.CRITICAL)

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def load_function():
    """Pull the registration coroutine out of __init__.py without importing it."""
    tree = ast.parse(SOURCE.read_text())
    node = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_async_register_frontend_resource"
        ),
        None,
    )
    if node is None:
        sys.exit("_async_register_frontend_resource not found in __init__.py")
    module = ast.Module(body=[node], type_ignores=[])
    namespace: dict = {
        "LOGGER": logging.getLogger("test"),
        "FRONTEND_RESOURCE_URL": URL,
        "HomeAssistant": object,
    }
    exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), namespace)
    return namespace["_async_register_frontend_resource"]


register = load_function()


class Resources:
    """Stand-in for ResourceStorageCollection."""

    def __init__(self, items=None, create_raises=False):
        self.items = list(items or [])
        self.created: list[dict] = []
        self.create_raises = create_raises
        self.info_calls = 0

    async def async_get_info(self):
        self.info_calls += 1
        return {"resources": len(self.items)}

    def async_items(self):
        return self.items

    async def async_create_item(self, data):
        if self.create_raises:
            raise RuntimeError("storage is read-only")
        self.created.append(data)
        self.items.append(data)
        return data


class Hass:
    def __init__(self, data):
        self.data = data


def lovelace(mode="storage", **kwargs):
    obj = type("LovelaceData", (), {})()
    obj.resource_mode = mode
    obj.resources = Resources(**kwargs)
    return obj


def run(hass):
    asyncio.run(register(hass))


def main() -> int:
    print("\n_async_register_frontend_resource")

    # Lovelace missing entirely — must not raise.
    run(Hass({}))
    check(True, "no lovelace present is survivable")

    # Storage mode, nothing registered yet.
    ll = lovelace()
    run(Hass({"lovelace": ll}))
    check(
        ll.resources.created == [{"res_type": "module", "url": URL}],
        "registers the module when absent",
    )

    # Runs on every setup, so it must not add a second copy.
    ll = lovelace(items=[{"res_type": "module", "url": URL}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [], "does not add a duplicate")

    # HA appends a cache-buster to resources; that is still the same resource.
    ll = lovelace(items=[{"res_type": "module", "url": f"{URL}?hacstag=123"}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [], "matches an existing entry with a query string")

    # An unrelated resource must not be mistaken for ours.
    ll = lovelace(items=[{"res_type": "module", "url": "/local/other-card.js"}])
    run(Hass({"lovelace": ll}))
    check(len(ll.resources.created) == 1, "unrelated resources are ignored")

    # YAML mode is read-only: warn, never write.
    ll = lovelace(mode="yaml")
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [], "YAML mode does not attempt a write")
    check(ll.resources.info_calls == 0, "YAML mode does not even load resources")

    # A failure to create must not propagate and break setup.
    ll = lovelace(create_raises=True)
    try:
        run(Hass({"lovelace": ll}))
        raised = False
    except Exception:
        raised = True
    check(not raised, "a storage failure never breaks config entry setup")

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
