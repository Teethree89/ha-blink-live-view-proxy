# Dashboard Guide

Three ways to build a camera wall, from most manual to fully automatic. All
three give you the same four actions per camera.

## Register the dashboard resource first

Nothing below works until this is registered, and when it is missing it fails
**silently** — the tile is there, the tap does nothing, and there is no console
error, no log line, and no failed request to point you at the cause.

Settings → Dashboards → ⋮ → Resources → Add:

```
URL:  /api/blink_liveview_proxy/static/blink-liveview-dialog.js
Type: JavaScript module
```

If a tap does nothing at all, check this before anything else.

## What each camera can do

| Action | Fired as | Needs |
|---|---|---|
| Live view | `blink_liveview_proxy` | `slug` |
| Local clips | `blink_liveview_proxy_clips` | `slug` |
| Fresh snapshot | `blink_snapshot_refresh` | `slug` |
| Motion on/off | `switch.toggle` | official Blink integration |

`entity_id` and `source_entity_id` are optional everywhere — they default to
`camera.blink_live_<slug>` and `camera.<slug>`. Pass them when your entities
were renamed.

Push-to-talk, **End & Save** and **Save MP4** are inside the live-view dialog,
not on the tile. PTT only appears for cameras the proxy reports as supporting
it; Mini/`owl` cameras are hidden by default and can be opted back in per
camera with `ptt_force_enabled_slugs`.

## Option 1 — Copy one camera and edit

[`examples/lovelace-dashboard.yaml`](../examples/lovelace-dashboard.yaml)

One fully commented camera with all four actions. Copy the block per camera and
change the slug. Needs **button-card** only.

Best if you want to restyle things, or only have a couple of cameras.

## Option 2 — Generate it from the proxy

[`scripts/generate-dashboard.py`](../scripts/generate-dashboard.py)

Asks the running proxy what exists and prints a finished dashboard:

```bash
python3 scripts/generate-dashboard.py > cameras.yaml
python3 scripts/generate-dashboard.py --proxy-url http://homeassistant.local:8088
python3 scripts/generate-dashboard.py --token "$BLINK_PROXY_TOKEN"
```

Paste the output into the dashboard's raw configuration editor. Needs
**button-card** only, and no template rendering at view time.

Re-run it after adding a camera — the output is a snapshot, not a live view.

Find your slugs the same way it does:

```bash
curl http://127.0.0.1:8088/cameras
```

## Option 3 — Populate itself

[`examples/lovelace-auto-populate.yaml`](../examples/lovelace-auto-populate.yaml)

Builds a tile for every `camera.blink_live_*` entity as it appears. Add a camera
in Blink, restart the proxy, and it shows up with no YAML edits.

Needs **auto-entities** and **button-card**.

It works because the integration puts everything a card needs onto the camera
entity as attributes, so the template reads them back instead of you typing
them:

| Attribute | Use |
|---|---|
| `proxy_slug` | the slug in every proxy URL |
| `blink_entity_id` | official Blink camera, for the thumbnail |
| `ptt_supported` | whether PTT is offered |
| `camera_type` | `default`, `doorbell`, `mini` |
| `product_type` | `catalina`, `lotus`, `owl`, `xt` … |

To show only some cameras, filter on those attributes — for example only the
ones that can talk back:

```jinja
{%- for s in states.camera
      if s.entity_id.startswith('camera.blink_live_')
      and state_attr(s.entity_id, 'ptt_supported') -%}
```

## Motion detection entity names

The motion switch belongs to the **official** Blink integration, and its name
comes from the camera's name there, not from the proxy slug. Options 2 and 3
guess `switch.<slug>_camera_motion_detection`. When a camera was renamed on one
side only, that guess is wrong and the button shows as unavailable — fix the
entity id or delete that element.

## Status pills and the reload button

All three options open with a pill row: proxy up/down, `N of M` cameras
discovered, an overall health pill, and a reload button.

The middle two read a REST sensor that polls the proxy's `/status`, defined in
[`examples/homeassistant-package.yaml`](../examples/homeassistant-package.yaml).
Install that package to light them up; without it they show as unavailable and
the proxy pill still works on its own.

The reload button calls `script.blink_reload_guarded`, which **refuses to run**
unless a reload could actually help. Hold it to force. That is not
over-engineering — a reload re-runs the full Blink login, and an unguarded
reload loop will text you until the account is rate-limited. The reasoning and
the incident behind it are in [OPERATIONS.md](OPERATIONS.md).

## Wall panels

On low-power Android panels, tap-to-toggle talk is more reliable than
press-and-hold: the WebView is decoding video and capturing microphone audio at
the same time. Browser microphone capture also needs a trusted HTTPS origin —
see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).
