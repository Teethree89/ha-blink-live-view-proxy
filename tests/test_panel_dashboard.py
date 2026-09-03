"""Static and pure-function checks for the admin dashboard."""

from __future__ import annotations

import ast
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components/blink_liveview_proxy"
FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def test_yaml() -> None:
    print("\npanel YAML")
    sys.path.insert(0, str(COMPONENT))
    from dashboard_yaml import render_dashboard_yaml

    cameras = [
        {
            "slug": "front_door",
            "name": 'Front: "Door"',
            "entity_id": "camera.front_door",
            "live_entity_id": "camera.blink_live_front_door",
            "ptt_supported": True,
            "entities": [
                {
                    "domain": "switch",
                    "entity_id": "switch.front_door_motion_detection",
                }
            ],
        },
        {
            "slug": "back_yard",
            "name": "Back Yard",
            "entity_id": "camera.back_yard",
            "live_entity_id": "camera.blink_live_back_yard",
            "entities": [],
        },
    ]
    predicates = {
        "dashboard": lambda value: isinstance(value, dict) and "views" in value,
        "view": lambda value: isinstance(value, list) and value[0]["title"] == "Cameras",
        "card": lambda value: isinstance(value, dict) and value["type"] == "grid",
    }
    for output_format, predicate in predicates.items():
        text = render_dashboard_yaml(cameras, output_format)
        check(predicate(yaml.safe_load(text)), f"{output_format} output parses with the right root")
        check("blink_liveview_proxy" in text, f"{output_format} includes live-view actions")
        check("blink_liveview_proxy_clips" in text, f"{output_format} includes clips actions")
        check("blink_snapshot_refresh" in text, f"{output_format} includes snapshot actions")
        check("BLINK_PROXY_TOKEN" not in text, f"{output_format} never contains a proxy token")

    single = yaml.safe_load(render_dashboard_yaml(cameras[:1], "card"))
    check(single["type"] == "picture-elements", "one-camera card output is a bare tile")


def _decorators(node: ast.AsyncFunctionDef) -> set[str]:
    return {
        getattr(item, "id", "") or getattr(item, "attr", "")
        for item in node.decorator_list
    }


def test_backend_contract() -> None:
    print("\npanel backend")
    source = (COMPONENT / "views.py").read_text()
    tree = ast.parse(source)
    names = {
        "BlinkLiveviewProxyPanelView",
        "BlinkLiveviewProxyPanelUpdateView",
        "BlinkLiveviewProxyPanelYamlView",
    }
    classes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in names
    }
    check(set(classes) == names, "all three panel API views exist")
    for name, node in classes.items():
        handlers = [
            item
            for item in node.body
            if isinstance(item, ast.AsyncFunctionDef) and item.name in {"get", "post"}
        ]
        check(
            bool(handlers) and all("require_admin" in _decorators(item) for item in handlers),
            f"{name} is admin-only",
        )
    check("er.async_entries_for_device" in source, "camera devices expose all native entities")
    check("include_disabled_entities=True" in source, "disabled native entities remain discoverable")
    check("async_start_update(self.hass, entry_id)" in source, "the panel uses the shared updater")

    repairs = (COMPONENT / "repairs.py").read_text()
    check("async_start_update(self.hass, self._entry_id)" in repairs, "Repairs uses the shared updater")

    check('"prerequisites"' in source, "the payload carries the prerequisite readout")
    check("prerequisites.build(" in source, "the readout comes from the tested module")
    # Lovelace may not have started when the panel is first opened. Reporting
    # "missing" in that window would send people to fix a correct install.
    check("return None" in source.split("async def _lovelace_resource_urls")[1].split("async def")[0],
          "an unreadable Lovelace resource list is reported as unknown")

    # The brand images live beside manifest.json, not in frontend/, and Home
    # Assistant only serves them itself from 2026.3.0. The floor is 2024.6.0.
    check("BRAND_ROOT" in source, "the panel serves its own brand images")
    for name in ("logo.png", "dark_logo.png"):
        check(f'"{name}"' in source, f"{name} is on the static allow-list")


def test_frontend_contract() -> None:
    print("\npanel frontend")
    panel = (COMPONENT / "frontend/blink-proxy-auth-panel.js").read_text()
    for label in ("Overview", "Cameras & entities", "Authentication", "YAML"):
        check(label in panel, f"the {label} tab is present")
    check('CustomEvent("hass-more-info"' in panel, "entity rows open native More Info")
    check("blink_liveview_proxy_clips" in panel, "camera cards open local clips")
    check("blink_snapshot_refresh" in panel, "camera cards refresh snapshots")
    check("navigator.clipboard.writeText" in panel, "generated YAML has a copy action")
    check("window.confirm" in panel, "updates require explicit confirmation")
    check(
        'id="back"' in panel
        and "window.history.back()" in panel
        and 'CustomEvent("location-changed"' in panel,
        "the merged mobile back-button behavior is preserved",
    )

    check("Prerequisites" in panel, "the Overview tab carries the prerequisite readout")
    check("_prerequisitesHtml()" in panel, "and Overview actually renders it")
    # The whole point of the accordion: the steps are attached to the check,
    # not to its failure, so a green install can still read how it was built.
    check(
        "check.instructions.map" in panel and "<details data-help=" in panel,
        "every check carries its instructions, passing or not",
    )
    check("this._openHelp" in panel, "an open accordion survives a background poll")

    # The navy wordmark is close to invisible on Home Assistant's dark theme,
    # and a theme is not the same thing as prefers-color-scheme.
    check("_darkTheme()" in panel, "the wordmark follows the active theme")
    check(
        "dark_logo.png" in panel and "logo.png" in panel,
        "both wordmark variants are used",
    )
    check("_darkTheme()," in panel, "a theme change re-renders the header")


def main() -> int:
    test_yaml()
    test_backend_contract()
    test_frontend_contract()
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
