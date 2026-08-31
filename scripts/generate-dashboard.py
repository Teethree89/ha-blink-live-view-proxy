#!/usr/bin/env python3
"""Generate a Lovelace dashboard for every camera the proxy has discovered.

The auto-entities example builds the same wall live in the browser, but it
needs two custom cards and it re-renders on every state change. This asks the
proxy what exists once and prints plain YAML, so the only custom card you need
is button-card.

    python3 scripts/generate-dashboard.py > cameras.yaml
    python3 scripts/generate-dashboard.py --proxy-url http://ha.local:8088

Then paste the result into the dashboard's raw configuration editor.

Re-run it after adding a camera in Blink; nothing here watches for changes.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_PROXY = "http://127.0.0.1:8088"

# The "N of M" and "Health" pills read the REST sensor from
# examples/homeassistant-package.yaml. Without that package they show as
# unavailable; the proxy pill works on its own.
STATUS_PILLS = """\
      - type: horizontal-stack
        cards:
          - type: custom:button-card
            entity: binary_sensor.blink_liveview_proxy
            name: Proxy
            show_state: true
            state:
              - value: "on"
                color: "#22c55e"
                icon: mdi:cctv
              - value: "off"
                color: "#ef4444"
                icon: mdi:cctv-off
            styles:
              card: [[height, 74px]]
              name: [[font-size, 12px]]
          - type: custom:button-card
            entity: sensor.blink_cameras_discovered
            name: Cameras
            icon: mdi:camera-outline
            show_state: false
            show_label: true
            label: >
              [[[
                const found = entity ? entity.state : '?';
                const want = entity ? entity.attributes.configured : '?';
                return `${found} of ${want}`;
              ]]]
            styles:
              card: [[height, 74px]]
              name: [[font-size, 12px]]
              label: [[font-size, 15px], [font-weight, 600]]
          - type: custom:button-card
            entity: binary_sensor.blink_needs_attention
            name: Health
            show_state: true
            state:
              - value: "off"
                color: "#22c55e"
                icon: mdi:check-circle-outline
              - value: "on"
                color: "#f59e0b"
                icon: mdi:alert-decagram-outline
            styles:
              card: [[height, 74px]]
              name: [[font-size, 12px]]
          - type: custom:button-card
            name: Reload
            icon: mdi:refresh
            tap_action:
              action: call-service
              service: script.blink_reload_guarded
            hold_action:
              action: call-service
              service: script.blink_reload_force
            styles:
              card: [[height, 74px]]
              name: [[font-size, 12px]]
"""


def fetch_cameras(proxy_url: str, token: str) -> list[dict]:
    """Ask the proxy for its camera inventory."""
    request = urllib.request.Request(f"{proxy_url.rstrip('/')}/cameras")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise SystemExit(
                f"{proxy_url} rejected the request. The proxy has "
                "BLINK_PROXY_TOKEN set, so pass the same value with --token."
            ) from error
        raise SystemExit(f"{proxy_url} returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise SystemExit(
            f"Could not reach {proxy_url}: {error.reason}\n"
            "Is the proxy running, and is this the right host and port?"
        ) from error

    cameras = payload.get("cameras", [])
    if not cameras:
        raise SystemExit(
            "The proxy is up but reported no cameras. Check the `cameras` "
            "block in the proxy config, then restart it."
        )
    return cameras


def button(icon: str, right: str, event: str, body: str) -> str:
    """One round overlay button firing a dom event."""
    return f"""\
          - type: custom:button-card
            icon: {icon}
            show_name: false
            tap_action:
              action: fire-dom-event
              {event}:
{body}
            styles:
              card:
                - background: rgba(2, 6, 23, 0.55)
                - border-radius: 999px
                - border: 0
                - width: 36px
                - height: 36px
                - padding: 0
              icon:
                - color: white
                - width: 20px
            style:
              bottom: 8px
              right: {right}
              transform: none
              z-index: 2
"""


def card_for(camera: dict) -> str:
    """Render one picture-elements tile with all four actions."""
    slug = camera["slug"]
    name = camera.get("name") or slug.replace("_", " ").title()
    live = f"camera.blink_live_{slug}"
    source = camera.get("entity_id") or f"camera.{slug}"
    motion = f"switch.{slug}_camera_motion_detection"

    ptt = " (push-to-talk available)" if camera.get("ptt_supported") else ""
    header = f"      # {name} — {camera.get('product_type') or 'unknown'}{ptt}\n"

    tile = f"""\
      - type: picture-elements
        camera_image: {source}
        camera_view: auto
        aspect_ratio: 16x9
        elements:
          - type: custom:button-card
            entity: {live}
            show_icon: false
            show_name: false
            show_state: false
            tap_action:
              action: fire-dom-event
              blink_liveview_proxy:
                slug: {slug}
                entity_id: {live}
                title: {name}
            styles:
              card:
                - height: 100%
                - padding: 0
                - border: 0
                - box-shadow: none
                - background: rgba(0, 0, 0, 0)
            style:
              top: 0
              left: 0
              width: 100%
              height: 100%
              transform: none
              z-index: 1
          - type: custom:button-card
            show_icon: false
            show_state: false
            name: {name}
            styles:
              card:
                - background: rgba(0, 0, 0, 0)
                - border: 0
                - box-shadow: none
                - padding: 0
              name:
                - color: white
                - font-size: 14px
                - font-weight: 600
                - text-shadow: 0 1px 4px rgba(0, 0, 0, 0.8)
            style:
              bottom: 8px
              left: 12px
              transform: none
              z-index: 2
"""

    tile += button(
        "mdi:filmstrip",
        "100px",
        "blink_liveview_proxy_clips",
        f"                slug: {slug}\n"
        f"                title: {name} Clips",
    )
    tile += button(
        "mdi:image-refresh",
        "54px",
        "blink_snapshot_refresh",
        f"                slug: {slug}\n"
        f"                source_entity_id: {source}",
    )

    # Motion detection comes from the official Blink integration. The entity is
    # guessed from the slug, so it is commented out when we cannot be sure.
    tile += f"""\
          - type: custom:button-card
            entity: {motion}
            show_name: false
            icon: >
              [[[
                return entity && entity.state === 'on'
                  ? 'mdi:motion-sensor'
                  : 'mdi:motion-sensor-off';
              ]]]
            tap_action:
              action: toggle
            styles:
              card:
                - background: rgba(2, 6, 23, 0.55)
                - border-radius: 999px
                - border: 0
                - width: 36px
                - height: 36px
                - padding: 0
              icon:
                - width: 20px
                - color: >
                    [[[
                      return entity && entity.state === 'on'
                        ? '#38bdf8'
                        : '#94a3b8';
                    ]]]
            style:
              bottom: 8px
              right: 8px
              transform: none
              z-index: 2
"""
    return header + tile


def reindent(block: str, shift: int) -> str:
    """Shift a YAML block left or right, leaving blank lines alone.

    The blocks above are authored at the indentation a dashboard needs. A card
    pasted into the manual-card editor starts at column zero instead, so they
    get moved rather than rewritten.
    """
    if shift == 0:
        return block
    lines = []
    for line in block.splitlines():
        if not line.strip():
            lines.append("")
        elif shift > 0:
            lines.append(" " * shift + line)
        else:
            strippable = len(line) - len(line.lstrip(" "))
            lines.append(line[min(-shift, strippable):])
    return "\n".join(lines) + "\n"


HEADER_COMMON = """\
# Needs button-card, and this dashboard resource registered under
# Settings -> Dashboards -> Resources, as a JavaScript module:
#   /api/blink_liveview_proxy/static/blink-liveview-dialog.js
#
# The motion-detection button assumes the official Blink integration named its
# switch after the slug. Fix or delete any that show as unavailable.
"""

HEADER_DASHBOARD = """\
# This is a WHOLE DASHBOARD. The top-level `views:` key is the complete config
# for one dashboard, so pasting all of it replaces every view on whichever
# dashboard you paste it into.
#
#   Settings -> Dashboards -> + Add dashboard -> New dashboard from scratch,
#   open it, pencil -> ... -> Raw configuration editor, select all, paste, Save.
#
# To add it to a dashboard you already have, re-run with --format view.
"""

HEADER_VIEW = """\
# This is ONE VIEW, to add to a dashboard you already have.
#
#   Open the dashboard, pencil -> ... -> Raw configuration editor, then paste
#   this as another item under the `views:` key that is already there. Keep the
#   two-space indent on `- title:`.
"""

HEADER_CARD = """\
# This is a SINGLE CARD. It drops into any existing view without touching the
# rest of the dashboard.
#
#   Open the dashboard, pencil -> + Add card -> scroll down -> Manual,
#   replace what is in the box with this, Save.
"""


def emit_dashboard(cameras: list[dict]) -> str:
    return "views:\n" + emit_view(cameras)


def emit_view(cameras: list[dict]) -> str:
    out = "  - title: Cameras\n    path: cameras\n    cards:\n"
    out += STATUS_PILLS
    for camera in cameras:
        out += card_for(camera)
    return out


def unlist(block: str) -> str:
    """Turn a one-item YAML list block into a bare mapping.

    Tiles are authored as list items because that is what a view's `cards:`
    wants. The manual-card editor wants a mapping, so the first `- ` is
    dropped and the whole block pulled back two columns.
    """
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("- "):
            indent = len(line) - len(line.lstrip(" "))
            lines[index] = " " * (indent + 2) + line.lstrip(" ")[2:]
            break
    return reindent("\n".join(lines) + "\n", -2)


def emit_card(cameras: list[dict]) -> str:
    """One vertical-stack holding the pills and a grid of camera tiles."""
    if len(cameras) == 1:
        # A single camera needs no wrapper; the tile is already one card.
        return unlist(reindent(card_for(cameras[0]), -6))

    out = "type: vertical-stack\ncards:\n"
    out += reindent(STATUS_PILLS, -4)
    out += "  - type: grid\n    columns: 2\n    square: false\n    cards:\n"
    for camera in cameras:
        out += card_for(camera)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Lovelace dashboard, view, or card from the "
        "proxy's cameras.",
        epilog="Examples:\n"
        "  generate-dashboard.py > cameras.yaml\n"
        "  generate-dashboard.py --format card > card.yaml\n"
        "  generate-dashboard.py --format card --camera front_door\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY)
    parser.add_argument(
        "--token", default="", help="BLINK_PROXY_TOKEN, if the proxy requires one"
    )
    parser.add_argument(
        "--format",
        choices=("dashboard", "view", "card"),
        default="dashboard",
        help="dashboard: a whole dashboard (default). view: one view to add to "
        "an existing dashboard. card: a single card for the manual-card editor.",
    )
    parser.add_argument(
        "--camera",
        default="",
        metavar="SLUG",
        help="only this camera. With --format card that emits the bare tile.",
    )
    args = parser.parse_args()

    cameras = fetch_cameras(args.proxy_url, args.token)

    if args.camera:
        matched = [c for c in cameras if c["slug"] == args.camera]
        if not matched:
            known = ", ".join(sorted(c["slug"] for c in cameras))
            raise SystemExit(
                f"No camera with slug {args.camera!r}.\nKnown slugs: {known}"
            )
        cameras = matched

    cameras.sort(key=lambda item: item.get("name") or item["slug"])

    header = {
        "dashboard": HEADER_DASHBOARD,
        "view": HEADER_VIEW,
        "card": HEADER_CARD,
    }[args.format]
    body = {
        "dashboard": emit_dashboard,
        "view": emit_view,
        "card": emit_card,
    }[args.format](cameras)

    print("# Generated by scripts/generate-dashboard.py")
    print(f"# {len(cameras)} camera(s) from {args.proxy_url}")
    print("#")
    print(header, end="")
    print("#")
    print(HEADER_COMMON, end="")
    print(body, end="")

    missing = [c["slug"] for c in cameras if not c.get("entity_id")]
    if missing:
        print(
            "\n# No entity_id configured for: " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "# Their thumbnails fall back to camera.<slug> and may not exist.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
