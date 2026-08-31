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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Lovelace dashboard from the proxy's cameras."
    )
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY)
    parser.add_argument(
        "--token", default="", help="BLINK_PROXY_TOKEN, if the proxy requires one"
    )
    args = parser.parse_args()

    cameras = fetch_cameras(args.proxy_url, args.token)
    cameras.sort(key=lambda item: item.get("name") or item["slug"])

    print("# Generated by scripts/generate-dashboard.py")
    print(f"# {len(cameras)} camera(s) from {args.proxy_url}")
    print("#")
    print("# Needs button-card, and this dashboard resource:")
    print("#   /api/blink_liveview_proxy/static/blink-liveview-dialog.js")
    print("#")
    print("# The motion-detection button assumes the official Blink")
    print("# integration named its switch after the slug. Fix or delete any")
    print("# that show as unavailable.")
    print("views:")
    print("  - title: Cameras")
    print("    path: cameras")
    print("    cards:")
    print("      - type: entities")
    print("        title: Blink Liveview Proxy")
    print("        entities:")
    print("          - entity: binary_sensor.blink_liveview_proxy")
    print("            name: Proxy")

    for camera in cameras:
        print(card_for(camera), end="")

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
