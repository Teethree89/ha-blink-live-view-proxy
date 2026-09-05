"""Render copy-ready Lovelace YAML from the discovered camera inventory."""

from __future__ import annotations

from typing import Any


def _scalar(value: Any) -> str:
    """Quote a YAML scalar conservatively without taking a PyYAML dependency."""
    text = str(value or "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _camera_card(camera: dict[str, Any], indent: int = 6) -> str:
    """Return one camera tile with live view, clips, snapshot, and motion."""
    slug = str(camera.get("slug") or "camera")
    name = str(camera.get("name") or slug.replace("_", " ").title())
    source = str(camera.get("entity_id") or f"camera.{slug}")
    live = str(camera.get("live_entity_id") or f"camera.blink_live_{slug}")
    motion = next(
        (
            str(item.get("entity_id"))
            for item in camera.get("entities", [])
            if item.get("domain") in {"switch", "binary_sensor"}
            and "motion" in str(item.get("entity_id", ""))
        ),
        f"switch.{slug}_camera_motion_detection",
    )
    style_anchor = "round_" + "".join(
        character if character.isalnum() else "_" for character in slug
    )
    pad = " " * indent
    lines = [
        f"{pad}- type: picture-elements",
        f"{pad}  camera_image: {_scalar(source)}",
        f"{pad}  camera_view: auto",
        f"{pad}  aspect_ratio: 16x9",
        f"{pad}  elements:",
        f"{pad}    - type: custom:button-card",
        f"{pad}      entity: {_scalar(live)}",
        f"{pad}      show_icon: false",
        f"{pad}      show_name: false",
        f"{pad}      tap_action:",
        f"{pad}        action: fire-dom-event",
        f"{pad}        blink_liveview_proxy:",
        f"{pad}          slug: {_scalar(slug)}",
        f"{pad}          entity_id: {_scalar(live)}",
        f"{pad}          title: {_scalar(name)}",
        f"{pad}      styles:",
        f"{pad}        card:",
        f"{pad}          - height: 100%",
        f"{pad}          - padding: 0",
        f"{pad}          - border: 0",
        f"{pad}          - box-shadow: none",
        f"{pad}          - background: rgba(0, 0, 0, 0)",
        f"{pad}      style:",
        f"{pad}        top: 0",
        f"{pad}        left: 0",
        f"{pad}        width: 100%",
        f"{pad}        height: 100%",
        f"{pad}        transform: none",
        f"{pad}        z-index: 1",
        f"{pad}    - type: state-label",
        f"{pad}      entity: {_scalar(live)}",
        f"{pad}      prefix: {_scalar(name + ' · ')}",
        f"{pad}      style:",
        f"{pad}        left: 12px",
        f"{pad}        bottom: 12px",
        f"{pad}        transform: none",
        f"{pad}        color: white",
        f"{pad}        text-shadow: 0 1px 4px black",
        f"{pad}        z-index: 2",
        f"{pad}    - type: custom:button-card",
        f"{pad}      icon: mdi:filmstrip",
        f"{pad}      show_name: false",
        f"{pad}      tap_action:",
        f"{pad}        action: fire-dom-event",
        f"{pad}        blink_liveview_proxy_clips:",
        f"{pad}          slug: {_scalar(slug)}",
        f"{pad}          title: {_scalar(name + ' Clips')}",
        # 40px is Home Assistant's own icon-button size and the smallest that
        # is comfortable under a thumb; the three sit 48px apart.
        f"{pad}      styles: &{style_anchor}",
        f"{pad}        card:",
        f"{pad}          - background: rgba(2, 6, 23, 0.55)",
        f"{pad}          - border-radius: 999px",
        f"{pad}          - border: 0",
        f"{pad}          - width: 40px",
        f"{pad}          - height: 40px",
        f"{pad}          - padding: 0",
        f"{pad}        icon:",
        f"{pad}          - color: white",
        f"{pad}          - width: 22px",
        f"{pad}      style:",
        f"{pad}        right: 104px",
        f"{pad}        bottom: 8px",
        f"{pad}        transform: none",
        f"{pad}        z-index: 2",
        f"{pad}    - type: custom:button-card",
        f"{pad}      icon: mdi:image-refresh",
        f"{pad}      show_name: false",
        f"{pad}      tap_action:",
        f"{pad}        action: fire-dom-event",
        f"{pad}        blink_snapshot_refresh:",
        f"{pad}          slug: {_scalar(slug)}",
        f"{pad}          source_entity_id: {_scalar(source)}",
        f"{pad}      styles: *{style_anchor}",
        f"{pad}      style:",
        f"{pad}        right: 56px",
        f"{pad}        bottom: 8px",
        f"{pad}        transform: none",
        f"{pad}        z-index: 2",
        f"{pad}    - type: custom:button-card",
        f"{pad}      entity: {_scalar(motion)}",
        f"{pad}      icon: mdi:motion-sensor",
        f"{pad}      show_name: false",
        f"{pad}      tap_action:",
        f"{pad}        action: toggle",
        f"{pad}      styles: *{style_anchor}",
        f"{pad}      style:",
        f"{pad}        right: 8px",
        f"{pad}        bottom: 8px",
        f"{pad}        transform: none",
        f"{pad}        z-index: 2",
    ]
    return "\n".join(lines) + "\n"


def _proxy_pill(indent: int) -> str:
    """The one status pill that works with the integration alone.

    The same mark in both states, coloured green or red rather than swapped for
    a different glyph. A crossed-out camera reads as "off" faster in isolation,
    but beside the live state it is a different object entirely, and the pill
    stops looking like this integration exactly when someone is staring at it.
    Nothing is lost by colouring instead: show_state prints the word underneath,
    so the state does not rest on colour alone.
    """
    pad = " " * indent
    lines = [
        f"{pad}- type: custom:button-card",
        f"{pad}  entity: binary_sensor.blink_liveview_proxy",
        f"{pad}  name: Proxy",
        f"{pad}  show_state: true",
        f"{pad}  icon: blink:logo",
        f"{pad}  state:",
        f'{pad}    - value: "on"',
        f'{pad}      color: "#22c55e"',
        f"{pad}      icon: blink:logo",
        f'{pad}    - value: "off"',
        f'{pad}      color: "#ef4444"',
        f"{pad}      icon: blink:logo",
        f"{pad}  styles:",
        f"{pad}    card:",
        f"{pad}      - height: 74px",
        f"{pad}    name:",
        f"{pad}      - font-size: 12px",
    ]
    return "\n".join(lines) + "\n"


def _sections_view(cameras: list[dict[str, Any]]) -> str:
    """One view in the sections layout: a section per camera.

    Sections lay themselves out by width - up to max_columns side by side on a
    desktop, a single column on a phone - which is what a fixed-column grid
    never did. Each camera is its own section so the tiles are what flow;
    the pill row spans the full width above them.
    """
    out = (
        "  - title: Cameras\n"
        "    path: cameras\n"
        "    type: sections\n"
        "    max_columns: 3\n"
        "    sections:\n"
        "      - type: grid\n"
        "        column_span: 3\n"
        "        cards:\n"
        "          - type: horizontal-stack\n"
        "            cards:\n"
    )
    out += _proxy_pill(14)
    for camera in cameras:
        out += "      - type: grid\n        cards:\n"
        out += _camera_card(camera, 10)
    return out


def render_dashboard_yaml(
    cameras: list[dict[str, Any]], output_format: str = "dashboard"
) -> str:
    """Render a dashboard, one view, or a card for the supplied cameras."""
    if output_format not in {"dashboard", "view", "card"}:
        raise ValueError("format must be dashboard, view, or card")
    cameras = sorted(cameras, key=lambda item: item.get("name") or item.get("slug"))
    header = (
        "# Generated by Blink Live View Proxy. Requires custom:button-card.\n"
        "# The integration registers blink-liveview-dialog.js automatically in "
        "storage-mode Lovelace.\n"
    )
    if output_format == "dashboard":
        return header + "views:\n" + _sections_view(cameras)
    if output_format == "view":
        return header + _sections_view(cameras)
    if len(cameras) == 1:
        raw = _camera_card(cameras[0], 0).splitlines()
        raw[0] = raw[0].replace("- ", "", 1)
        raw[1:] = [line[2:] if line.startswith("  ") else line for line in raw[1:]]
        return header + "\n".join(raw) + "\n"
    body = "type: grid\ncolumns: 2\nsquare: false\ncards:\n"
    return header + body + "".join(_camera_card(camera, 2) for camera in cameras)
