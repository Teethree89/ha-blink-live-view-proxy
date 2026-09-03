"""Tests for registering the dialog module as a Lovelace resource.

No Home Assistant, no network. The function is loaded out of __init__.py by
source, because importing the package pulls in Home Assistant. Run from the
repo root:

    python tests/test_frontend_resource.py

This runs on every config-entry setup, so being idempotent matters more than
anything else here: a bug that adds the resource each time would quietly grow
the user's resource list on every restart.

It also has to find Lovelace at all. `hass.data["lovelace"]` was a plain dict
up to 2025.1, a LovelaceData dataclass with `mode` from 2025.2, and only from
2026.3 does that dataclass carry `resource_mode`. Reading `resource_mode`
directly found nothing on the first two, so this function returned early on
every core below 2026.3 and the registration silently never happened - which is
the exact failure it exists to prevent. Each shape is exercised below.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components/blink_liveview_proxy"
SOURCE = COMPONENT / "__init__.py"
URL = "/api/blink_liveview_proxy/static/blink-liveview-dialog.js"

sys.path.insert(0, str(COMPONENT))
import lovelace as lovelace_module  # noqa: E402

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
        # The real accessors, not stand-ins: reading Lovelace across its three
        # shapes is the half of this that was broken.
        "resource_collection": lovelace_module.resource_collection,
        "is_writable": lovelace_module.is_writable,
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


def lovelace(mode="storage", shape="modern", **kwargs):
    """Lovelace as one of the three things Home Assistant has stored there."""
    resources = Resources(**kwargs)
    if shape == "dict":  # 2024.6 to 2025.1
        return {"mode": mode, "resources": resources}
    obj = type("LovelaceData", (), {})()
    obj.resources = resources
    if shape == "dataclass":  # 2025.2 to 2026.2: mode, and no resource_mode
        obj.mode = mode
    else:  # 2026.3 onwards
        obj.mode = mode
        obj.resource_mode = mode
    return obj


def run(hass):
    asyncio.run(register(hass))


def main() -> int:
    print("\n_async_register_frontend_resource")

    # Lovelace missing entirely — must not raise.
    run(Hass({}))
    check(True, "no lovelace present is survivable")

    # Storage mode, nothing registered yet - in all three shapes, because a
    # shape this cannot read is a shape where nothing is ever registered.
    for shape, era in (
        ("dict", "2024.6 to 2025.1"),
        ("dataclass", "2025.2 to 2026.2"),
        ("modern", "2026.3 onwards"),
    ):
        data = lovelace(shape=shape)
        run(Hass({"lovelace": data}))
        resources = data["resources"] if isinstance(data, dict) else data.resources
        check(
            resources.created == [{"res_type": "module", "url": URL}],
            f"registers the module when absent, on {era}",
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

    # YAML mode is read-only: warn, never write. Also in all three shapes,
    # or an older core would be written to and raise where it is only logged.
    for shape, era in (
        ("dict", "2024.6 to 2025.1"),
        ("dataclass", "2025.2 to 2026.2"),
        ("modern", "2026.3 onwards"),
    ):
        data = lovelace(mode="yaml", shape=shape)
        run(Hass({"lovelace": data}))
        resources = data["resources"] if isinstance(data, dict) else data.resources
        check(resources.created == [], f"YAML mode does not write, on {era}")
        check(resources.info_calls == 0, f"YAML mode loads nothing, on {era}")

    # A shape with no mode at all is not assumed writable.
    unknown = type("LovelaceData", (), {})()
    unknown.resources = Resources()
    run(Hass({"lovelace": unknown}))
    check(unknown.resources.created == [], "an unreported mode is never written to")

    print("\nreading Lovelace across its three shapes")
    read = lovelace_module.resource_mode
    check(read({"mode": "storage"}) == "storage", "the 2024.6 dict reports its mode")
    check(read(lovelace(shape="dataclass")) == "storage",
          "the 2025.2 dataclass reports mode when it has no resource_mode")
    # From 2026.3 a storage-mode dashboard can still take resources from YAML,
    # and resource_mode is the only field that says so.
    split = lovelace(shape="modern")
    split.resource_mode = "yaml"
    check(read(split) == "yaml", "resource_mode wins over mode where both exist")
    check(read(None) is None and read(object()) is None,
          "an unreadable Lovelace reports no mode rather than guessing one")
    check(lovelace_module.resource_collection({"resources": 1}) == 1,
          "the dict's resource collection is reachable")

    print("\n_async_register_frontend_resource, continued")

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
