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
URL = "/api/blink_liveview_proxy/assets/blink-liveview-dialog.js"
LEGACY_URL = "/api/blink_liveview_proxy/static/blink-liveview-dialog.js"

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
            and n.name == "_async_try_register_resource"
        ),
        None,
    )
    if node is None:
        sys.exit("_async_try_register_resource not found in __init__.py")
    module = ast.Module(body=[node], type_ignores=[])
    namespace: dict = {
        "LOGGER": logging.getLogger("test"),
        "FRONTEND_RESOURCE_URL": URL,
        "LEGACY_FRONTEND_RESOURCE_URL": LEGACY_URL,
        "HomeAssistant": object,
        # The real accessors, not stand-ins: reading Lovelace across its three
        # shapes is the half of this that was broken.
        "resource_collection": lovelace_module.resource_collection,
        "is_writable": lovelace_module.is_writable,
    }
    exec(compile(ast.fix_missing_locations(module), "__init__.py", "exec"), namespace)
    return namespace["_async_try_register_resource"]


register = load_function()


class Resources:
    """Stand-in for ResourceStorageCollection."""

    def __init__(self, items=None, create_raises=False):
        self.items = list(items or [])
        self.created: list[dict] = []
        self.updated: list[tuple] = []
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

    async def async_update_item(self, item_id, updates):
        self.updated.append((item_id, updates))
        for item in self.items:
            if item.get("id") == item_id:
                item.update(updates)
                return item
        return None


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


VERSION = "0.7.0"
VERSIONED = f"{URL}?v={VERSION}"


def run(hass, version=VERSION):
    """Run one attempt, returning whether Lovelace could be reached at all."""
    return asyncio.run(register(hass, version))


def main() -> int:
    print("\n_async_register_frontend_resource")

    # Lovelace missing entirely — must not raise, and must report that it did
    # not get an answer, so the caller knows to try again. Returning "done"
    # here is what left installs with no resource and nothing in the log.
    check(run(Hass({})) is False, "an absent Lovelace is retryable, not final")

    # A shape whose resources cannot be reached is the same retryable case.
    empty = type("LovelaceData", (), {})()
    check(run(Hass({"lovelace": empty})) is False, "no resource collection is retryable")

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
            resources.created == [{"res_type": "module", "url": VERSIONED}],
            f"registers the module when absent, on {era}",
        )

    # Runs on every setup, so it must not add a second copy.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": VERSIONED}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [], "does not add a duplicate")

    # HA appends a cache-buster to resources; that is still the same resource.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": f"{URL}?hacstag=123"}])
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

    print("\nmigrating off the service-worker-cached path")

    # The pre-0.6.2 URL is rewritten in place. Creating the new one alongside
    # would load the module twice and leave a stale entry nobody knows to
    # remove — and the stale one is the copy the service worker holds forever.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": LEGACY_URL}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [], "no second entry is added")
    check(ll.resources.updated == [("abc", {"url": VERSIONED})], "the old entry is rewritten")
    check(
        [item["url"] for item in ll.resources.items] == [VERSIONED],
        "the resource list ends up with exactly one, on the new path",
    )

    # Already migrated: nothing to do, and nothing to churn.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": VERSIONED}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [] and ll.resources.updated == [],
          "an already-migrated list is left alone")

    # HACS-style cache-buster on the legacy entry is still the legacy entry.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": f"{LEGACY_URL}?hacstag=1"}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.updated == [("abc", {"url": VERSIONED})],
          "a legacy entry with a query string is migrated too")

    # An entry with no id cannot be updated; fall back to creating the new one
    # rather than raising through config entry setup.
    ll = lovelace(items=[{"res_type": "module", "url": LEGACY_URL}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [{"res_type": "module", "url": VERSIONED}],
          "an un-updatable legacy entry still gets the new resource added")

    print("\nthe version in the URL, which is what reaches a stale browser")

    # An upgrade has to change the URL. no-cache and an ETag make a browser
    # revalidate, but a module already imported by a live document stays in
    # that document's registry - and the companion app keeps its webview
    # alive across app switches, so nothing else dislodges the old code.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": f"{URL}?v=0.6.2"}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.updated == [("abc", {"url": VERSIONED})],
          "an older version is moved to the current one")
    check(ll.resources.created == [], "and not added a second time")

    # Same version: nothing to do, and nothing to churn on every setup.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": VERSIONED}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.updated == [] and ll.resources.created == [],
          "the current version is left completely alone")

    # An unversioned entry from before this existed is brought up to date.
    ll = lovelace(items=[{"id": "abc", "res_type": "module", "url": URL}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.updated == [("abc", {"url": VERSIONED})],
          "an entry predating the version query gets one")

    # No id means it cannot be updated; it must still not be duplicated.
    ll = lovelace(items=[{"res_type": "module", "url": f"{URL}?v=0.6.2"}])
    run(Hass({"lovelace": ll}))
    check(ll.resources.created == [], "an un-updatable entry is never duplicated")

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
