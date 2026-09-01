# Dashboard Guide

Three ways to build a camera wall, from most manual to fully automatic. All
three give you the same four actions per camera.

## The dashboard resource

The integration registers this for you when the config entry is set up:

```
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
```

Nothing below works without it, and when it is missing it fails **silently** —
the tile is there, the tap does nothing, and there is no console error, no log
line, and no failed request to point you at the cause. So if a tap does nothing
at all, check this before anything else, under
Settings → Dashboards → ⋮ → Resources.

Two cases still need it added by hand, as a **JavaScript module**:

- **YAML-mode Lovelace**, where the resource list comes from
  `configuration.yaml` and cannot be written to. The log says so on startup.
- Anyone who removed it.

## Where to paste it

All three options produce a **whole dashboard**, not a single card. The
top-level `views:` key is the complete configuration for one dashboard, so
pasting the lot **replaces every view** on the dashboard you paste it into.

**Onto a new dashboard** — recommended, nothing to lose:

1. Settings → Dashboards → **+ Add dashboard** → **New dashboard from scratch**
2. Open it, click the pencil to edit, then **⋮ → Raw configuration editor**
3. Select all, delete, paste the file, **Save**

**Onto a dashboard you already have** — paste only the view, not the wrapper:

```yaml
views:
  - title: Home          # your existing view, leave it
    cards: [...]

  - title: Cameras       # <- paste from here down
    path: cameras
    cards:
      ...
```

Copy from the `- title: Cameras` line down and drop it under the `views:` key
that is already there. Do not paste the `views:` line itself, and keep the
two-space indent on `- title:` or the editor will reject it.

If **⋮** offers "Take control" instead of a raw editor, the dashboard is still
auto-generated. Take control first — that is a one-way change.

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

Asks the running proxy what exists and prints finished YAML. `--format`
decides what shape, so you are not forced to replace a dashboard just to get
the cameras in:

```bash
# a whole dashboard (default)
python3 scripts/generate-dashboard.py > cameras.yaml

# one view, to add to a dashboard you already have
python3 scripts/generate-dashboard.py --format view

# a single card for the manual-card editor, nothing else touched
python3 scripts/generate-dashboard.py --format card

# just one camera, as a bare card
python3 scripts/generate-dashboard.py --format card --camera front_door
```

| `--format` | Produces | Paste into |
|---|---|---|
| `dashboard` | `views:` — a complete dashboard | Raw configuration editor, replacing everything |
| `view` | one `- title: Cameras` item | Raw configuration editor, under the existing `views:` |
| `card` | one `vertical-stack` card | + Add card → Manual |
| `card --camera SLUG` | one `picture-elements` tile | + Add card → Manual |

By default the output carries only the **Proxy** pill, because that is the one
that works with the integration alone. Add `--with-package` for the Cameras,
Health and Reload pills once you have installed
[`examples/homeassistant-package.yaml`](../examples/homeassistant-package.yaml)
— without it those two read a sensor that does not exist and Reload calls a
script that does not exist, so it looks fine and silently does nothing.

Other options: `--proxy-url http://homeassistant.local:8088` and
`--token "$BLINK_PROXY_TOKEN"` if the proxy requires one.

`--demo` builds from a made-up four-camera inventory and contacts nothing, so
you can see the output shapes without a proxy running. The checked-in
`examples/generated-*.yaml` files are produced that way, which is why they show
`front_door` and `kitchen` rather than anyone's real cameras.

Every shape carries its own paste instructions in the printed header. Needs
**button-card** only, and nothing renders at view time.

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

The dashboards open with a pill row: proxy up/down, `N of M` cameras
discovered, an overall health pill, and a reload button.

**Only the Proxy pill works on its own.** The other three need
[`examples/homeassistant-package.yaml`](../examples/homeassistant-package.yaml):
Cameras and Health read its REST sensor, and Reload calls its guarded script.
Without that package the first two render as a stub and Reload looks perfectly
normal while doing nothing at all.

So either install the package, or delete those three pills. The generator
leaves them out unless you pass `--with-package`.

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
