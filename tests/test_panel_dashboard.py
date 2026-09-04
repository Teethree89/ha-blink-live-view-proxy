"""Static and pure-function checks for the admin dashboard."""

from __future__ import annotations

import ast
import pathlib
import re
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
    # Assistant only serves them itself from 2026.3.0. The floor is 2024.11.0.
    # hass.data["lovelace"] has had three shapes; reading it directly saw only
    # the newest, which is why both callers go through the same accessor.
    check("resource_collection(" in source, "Lovelace is read through the shared accessor")
    init = (COMPONENT / "__init__.py").read_text()
    check("resource_collection(lovelace)" in init and "is_writable(lovelace)" in init,
          "registration reads Lovelace the same way the readout does")

    # Every asset URL in the wild points at the old path, and a 404 there is
    # exactly the silent dead dashboard this area exists to prevent.
    check("BlinkLiveviewProxyLegacyAssetView" in source,
          "the superseded asset path is still answered")
    check("_hacs_update_facts" in source, "the readout can see HACS's update entity")
    check("release_url" in source,
          "HACS's entity is matched on the repository URL, not a renameable name")

    # The panel is in the sidebar from the first restart after installing,
    # before any config entry exists. A 503 there was the one thing it showed.
    check('"configured": False' in source and '"configured": True' in source,
          "the payload says whether a config entry exists yet")
    check("except web.HTTPServiceUnavailable:" in source.split("async def _panel_payload")[1].split("PANEL_UPDATE_MESSAGES")[0],
          "a missing entry is answered, not raised")
    check('"blink-liveview-icons.js"' in source, "the icon set is on the static allow-list")
    check("BlinkLiveviewProxyClipThumbnailView" in source, "clip thumbnails are proxied")
    check('"thumbnail_url"' in source, "and their URLs are rewritten like the download URL")
    check('range_header := request.headers.get("Range")' in source,
          "byte ranges reach the proxy, so the clip player can seek and Safari can play")

    check("BRAND_ROOT" in source, "the panel serves its own brand images")
    for name in ("logo.png", "dark_logo.png"):
        check(f'"{name}"' in source, f"{name} is on the static allow-list")


def test_placeholder_substitution() -> None:
    """Every __PLACEHOLDER__ in views.py must actually be substituted.

    The clip viewer's HTML is a plain string, not an f-string - its CSS is full
    of unescaped braces - so it fills in values with .replace() instead. A
    placeholder added without its replace ships to the browser verbatim, and an
    f-string brace added to that template does nothing at all. Neither raises.
    """
    print("\ntemplate placeholders")
    source = (COMPONENT / "views.py").read_text()
    names = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", source)))
    check(bool(names), "the plain-string templates still use placeholders")
    for name in names:
        check(
            f'.replace("{name}"' in source,
            f"{name} is substituted rather than shipped literally",
        )


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

    # Home Assistant loads Lovelace resources only on a Lovelace dashboard, so
    # a session that came straight to this panel has nothing listening for the
    # events the Cameras tab fires.
    check("_ensureDialog()" in panel, "the panel loads the dialog helper itself")
    check("__blinkLiveviewDialogLoaded" in panel,
          "and defers to a dashboard that already loaded it")

    check("_updateBannerHtml()" in panel, "a started update reports its progress")
    check("UPDATE_TIMEOUT_MS" in panel, "and gives up rather than spinning forever")
    check('id="update-reload"' in panel and "window.location.reload()" in panel,
          "the reload after an update is offered, not forced")
    check("window.location.reload();\n" not in panel.split("_updateStatus()")[0],
          "nothing reloads the page on its own")

    # The navy wordmark is close to invisible on Home Assistant's dark theme,
    # and a theme is not the same thing as prefers-color-scheme.
    check("_darkTheme()" in panel, "the wordmark follows the active theme")
    check(
        "dark_logo.png" in panel and "logo.png" in panel,
        "both wordmark variants are used",
    )
    check("_darkTheme()," in panel, "a theme change re-renders the header")

    check("_setupHtml()" in panel and "configured === false" in panel,
          "before a config entry exists, Overview shows the install paths")
    check("/config/integrations/dashboard/add?domain=blink_liveview_proxy" in panel,
          "and offers to start the config flow")
    for tab in ("_camerasHtml", "_authHtml", "_yamlHtml"):
        body = panel.split(f"  {tab}() {{")[1].split("\n  }\n")[0]
        check("_configured()" in body, f"{tab} explains itself before setup rather than erroring")

    init = (COMPONENT / "__init__.py").read_text()
    check('sidebar_icon="blink:logo"' in init and "add_extra_js_url" in init,
          "the sidebar entry uses the shipped icon set, loaded on every page")
    check('sidebar_title="Blink Live View Proxy"' in init, "the sidebar entry carries the product name")


def main() -> int:
    test_yaml()
    test_backend_contract()
    test_placeholder_substitution()
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
