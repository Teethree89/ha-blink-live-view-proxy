# Configuration

## Proxy Config

The proxy reads JSON from:

1. `--config /path/to/config.json`
2. `BLINK_PROXY_CONFIG`
3. `config.json` next to `blink_liveview_proxy.py`

Important fields:

```json
{
  "host": "127.0.0.1",
  "port": 8088,
  "auth_file": "/var/lib/blink-liveview-proxy/secrets/blink-auth.json",
  "ffmpeg": "ffmpeg",
  "liveview_cache_dir": "/var/lib/blink-liveview-proxy/liveviews",
  "clip_cache_dir": "/var/lib/blink-liveview-proxy/clips",
  "clip_cache_max_mb": 512,
  "mpegts_session_seconds": 60,
  "mpegts_cooldown_seconds": 30,
  "ptt_force_enabled_slugs": [],
  "ptt_disabled_camera_types": [],
  "ptt_disabled_product_types": ["xt", "white"],
  "cameras": {}
}
```

## Camera Map

The proxy can discover Blink cameras without a camera map. A map is still useful
for stable slugs, which every entity id and dashboard is built from.

Use `name`, `id`, or `serial` to match a Blink camera:

```json
{
  "cameras": {
    "front_door": {
      "name": "Front Door"
    }
  }
}
```

An `entity_id` key from older releases, which pointed at Home Assistant's own
Blink camera, is still accepted and no longer used.

## Blink Entities

The integration builds, from the proxy's own Blink session, what Home
Assistant's own Blink integration used to provide, so that integration is not
needed.

| Entity | Per | What it is |
|---|---|---|
| `camera.blink_proxy_<slug>` | camera | the latest Blink thumbnail |
| `button.blink_proxy_<slug>_refresh_snapshot` | camera | take a new one (wakes the camera) |
| `switch.blink_proxy_<slug>_motion_detection` | camera | the camera's motion detection |
| `binary_sensor.blink_proxy_<slug>_motion` | camera | motion since the previous refresh |
| `binary_sensor.blink_proxy_<slug>_battery` | camera | on when Blink calls the battery low |
| `sensor.blink_proxy_<slug>_temperature` | camera | °F as Blink reports it |
| `sensor.blink_proxy_<slug>_wifi_signal` | camera | diagnostic |
| `sensor.blink_proxy_<slug>_battery_voltage` | camera | diagnostic |
| `alarm_control_panel.blink_proxy_<sync>` | sync module | arm and disarm the system |
| `sensor.blink_liveview_proxy_last_blink_refresh` | proxy | when Blink was last refreshed, and why not |

`<slug>` is the proxy's camera slug and `<sync>` the sync module's name, lower
case with underscores. Every id starts `blink_proxy_`, so none collides with the
official integration's if it is still installed. Blink reports no temperature,
Wi-Fi or voltage for Doorbells and Minis, so those three are not created for
them. The live view's loading frame and the snapshot-refresh button use the
snapshot camera.

`Blink refresh interval in seconds`, under Configure (default `300`, range
`60-3600`), is how often the proxy refreshes Blink. Motion is as coarse as the
interval: a clip recorded between two refreshes shows up at the second one.
Switches, the alarm panel and the snapshot button update as soon as Blink
confirms them, and report an error when Blink accepts a change but does not
apply it.

The refresh never signs in. It renews the session with its refresh token and
nothing else, and if that fails it stops, marks the entities unavailable and
says why on the last-refresh sensor, rather than trying again on a timer. Other
failures back off, doubling up to an hour. The watchdog, if installed, stays
the only thing that restarts the proxy.

Needs a proxy from this release or newer; an older one has no `/devices`
route, and the integration logs that once and sets up with live view only.

## Live View Duration

The Home Assistant integration has an options flow:

```text
Settings > Devices & services > Blink Live View Proxy > Configure
```

`Live-view duration in seconds` controls how long the direct player asks the HA
route/proxy to keep each live view open. Valid range: `10-300` seconds.

Blink can still end sessions early.

## Live View Transports

Blink does not hand every camera the same kind of live-view URL, and the proxy
reads both. The transport is chosen per camera from whatever Blink returns —
there is nothing to configure, and which one a camera gets is Blink's decision
and can change under you.

- **`immis://`** — Blink's own framing, used by the newer battery cameras
  (`catalina`, `xt2`), by Mini/`owl`, and by `lotus` doorbells. This is the
  path the proxy has always taken.
- **`rtsps://`** — handed to the oldest generations, `xt` and `white`. Before
  0.5.0 these were rejected outright and had no live view at all.

The one user-visible difference is that **push-to-talk is not available on a
camera using the RTSP transport**: the transport carries no upstream audio
channel, so the proxy raises `push-to-talk is not available over RTSP` rather
than appearing to send audio nowhere. Everything else — the MSE player, HLS,
"End", snapshots, motion controls — behaves the same on both.

Blink's RTSP server does not follow RFC 2326: it answers every request with
`CSeq: 1` instead of echoing the sequence number, and omits both `Session` and
`Transport` on `SETUP`. Any one of those is fatal to a strict client, which is
why the proxy speaks the protocol itself rather than handing the URL to ffmpeg
— ffmpeg aborts with `CSeq 2 expected, 1 received` before the camera is ever
asked to wake, so the camera never lights up and nothing explains why.

## Start-up

The proxy copies Blink's stream into HLS segments unchanged — there is no
re-encode and no encoder cost per open stream.

Blink sends a keyframe every four seconds. A segment normally has to begin on
one, which used to make every segment four seconds long and left a player
waiting eight to twelve seconds after the tap before it had enough of the
playlist to start. The segmenter now runs with `split_by_time`, so segments are
cut on the clock at the one-second target (`-hls_time 1`) instead of on Blink's
keyframes. Only every fourth segment opens on a keyframe; a player that joins
mid-group waits for the next one, which is why the playlist window is eight
segments (`-hls_list_size 8`) rather than four.

Measured on an iPhone, the picture arrives about four seconds after the tap
instead of about twelve. Most of what is left is Blink waking the camera, which
nothing on this side can shorten.

An opt-in `hls_transcode` mode (add-on option `low_latency`) used to buy the
same head start by re-encoding through `libx264` to force a keyframe every
second. It was removed in 0.8.0: clock-cut segments get there without an
encoder, and the re-encode spent a 2 Mbit/s cap on restarting the picture every
second rather than on detail, which cost visible quality. A client uploading
push-to-talk audio at the same time could not always keep up with it either.
The add-on still accepts a stored `low_latency` so an existing install keeps
booting, but the value does nothing.

## ffmpeg Tuning

Four keys control how ffmpeg is invoked for HLS. The defaults are what the
measurements behind them produced, and there is normally no reason to change
any of them.

```json
{
  "ffmpeg": "ffmpeg",
  "ffmpeg_loglevel": "warning",
  "ffmpeg_probesize": 1000000,
  "ffmpeg_analyzeduration": 500000
}
```

`ffmpeg` is the binary to run. `ffmpeg_loglevel` is passed straight through to
`-loglevel`; raise it to `info` or `debug` when reading `ffmpeg.log` after a
live view failed to start.

`ffmpeg_probesize` and `ffmpeg_analyzeduration` bound how long ffmpeg inspects
the stream before writing anything — in bytes and in microseconds. ffmpeg's
MPEG-TS defaults are 5 MB and 5 seconds, whichever fills first, and Blink sends
well under a megabit, so the byte limit never filled and every live view paid
the full five seconds before its first segment existed. Bounding the window to
half a second of analysis has the first keyframe in hand about a second and a
half after `PLAY`.

The window is measured on packets whose duration ffmpeg already knows, which on
this stream means the audio. Widen both values if a camera ever sends something
the short window cannot identify.

One cosmetic effect worth knowing before you chase it in `ffmpeg.log`: on a
stream that opens with a gap, the window can end before the picture size is
known and ffmpeg logs `Could not find codec parameters ... unspecified size`.
On the default copy path that is harmless — segments are still written from the
first keyframe and play normally.

## Session Lifetime

An HLS session stays up while requests for its playlist or segments keep
arriving and is stopped `hls_idle_timeout` seconds (default `10`) after the last
one. A playing client asks for something at least once per segment, so the
timeout only fires once the player has gone. It is short on purpose: a Blink
camera streams for as long as the session exists, the official app stops the
camera the moment the viewer closes, and on a battery camera every extra idle
second is battery spent on nobody. A client that is still waiting for a slow
camera to start counts as active, so the idle timeout can be shorter than
`hls_start_timeout` (default `30`), the longest the proxy will wait for the
first segment before failing the request.

### `send_liveview_token`

Stock BlinkPy sends 64 null bytes in the auth-token field of the IMMI
handshake (see `blinkpy/livestream.py`, `get_auth_header()` — the code comment
literally says "64 null bytes for now"). `TokenAwareBlinkLiveStream` fixes
this by populating that field with the real `liveview_token` from the
liveview response.

Informal testing on a Blink Outdoor 4 (2K+, Sync Module 2, IMMI liveview)
found that individual segments consistently lasted longer with the real
token (roughly 3-8s per segment across a 100+ second run) than with the
stock zero-byte handshake (a very consistent ~2-2.5s cutoff across repeated
runs). This isn't a rigorous benchmark and Blink can still end sessions
early either way, but the pattern was consistent enough that `true` is now
the default. Set it back to `false` if you hit issues and want to compare.

## Push-to-Talk

**This is the one feature that needs an HTTPS address.** Browsers only expose
a microphone in a secure context — an HTTPS page, or `http://localhost` — so
Hold Talk cannot work from `http://<address>:8123`, which is Home Assistant's
own default and what the companion app uses at home unless you tell it
otherwise. How the proxy is reached makes no difference: the address in the
browser is what counts.

Today it fails badly rather than clearly. The button is enabled from whether
the *camera* supports push-to-talk, so on a plain-HTTP page it is offered,
looks live, and does nothing at all: the refusal is written to a status line
the player has already hidden by the time the button becomes usable, and
nothing appears in the proxy log because nothing was ever sent. If Hold Talk
seems dead, check **Blink Live View Proxy → Overview**, which has a row for
exactly this. Home Assistant Cloud (Nabu Casa) is the shortest route to an
HTTPS address; a reverse proxy with a certificate, or `ssl_certificate` and
`ssl_key` under `http:` in `configuration.yaml`, also do it.

Once the microphone is available, the player sends PCM to HA over WebSocket;
HA forwards it to the proxy; the proxy uses ffmpeg to encode AAC and sends
IMMI audio frames to Blink.

PTT is hidden for camera families in:

```json
"ptt_force_enabled_slugs": [],
"ptt_disabled_camera_types": [],
"ptt_disabled_product_types": ["xt", "white"]
```

`ptt_disabled_product_types` ships with `xt` and `white`; the other two lists
are empty.

**`xt` and `white` — never.** They get `rtsps://`, and `BlinkRtspLiveStream`
raises `NotImplementedError` because RTSP has no equivalent of
`send_session_command()`. There is nothing for the proxy to call, so the
button could only ever fail. No work on the proxy changes that.

`superior` used to be on the list for a different reason, and came off once
the capture that entry asked for existed. The audio shape was not the problem:
the encoder was emitting PNS frames, which the camera's decoder does not
handle. Confirmed audible on a Wired Floodlight, a `catalina` and an `xt2`.

That is a different case from `mini` and `owl`, which these lists used to
carry — set before anyone had actually tried it, and a Blink Mini/`owl` was
confirmed audible on June 30, 2026. The default was hiding a control that
works, and they came off.

`ptt_disabled_camera_types` stays empty on purpose: `camera_type` cannot tell
the families apart. An `xt` reports `default`, and so does a `catalina`, which
does have push-to-talk.

Put a family on a list when it genuinely cannot do this, and use
`ptt_force_enabled_slugs` for a single camera you want back regardless.

An add-on install can set all three; they are add-on options. There, an empty
box means "keep these defaults" rather than "allow everything", so the lists
can only be added to from the add-on UI.

## Clips

The HA clip viewer lists both inventories:

```text
/api/blink_liveview_proxy/clips/viewer
```

Its Source select narrows that to the Sync Module or to Blink's cloud alone,
and the browser remembers the choice for the next open. `?source=cloud` on the
URL opens straight there.

Cloud clips exist only for an account with a Blink subscription; without one,
motion clips are written to a Sync Module's local storage and nothing else.
Listing them is metadata only. Fetching one is not, and that is why cloud
clips show a placeholder rather than a thumbnail: a thumbnail is the first
frame of the clip, so drawing a screenful would pull every clip in the window
off Blink's servers. A cloud clip is fetched when someone plays it, or when
someone presses **Load cloud thumbnails**, which says how many clips that is
before it starts. The newest six cloud clips show a thumbnail automatically;
the rest load on demand. Local clips come off your own Sync Module and are
drawn without asking.

Each clip is fetched from Blink once and kept, and its first frame is cut as
the thumbnail the viewer shows. Two keys control that:

| Key | Default | What it does |
|---|---|---|
| `clip_cache_dir` | a `clips/` directory beside `liveview_cache_dir` | Where clips and thumbnails are kept. The add-on and the Docker image use `/data/clips`; a config written before this key existed lands under the same state directory as the live-view cache |
| `clip_cache_max_mb` | `512` | Past this, the oldest files go first. A thumbnail is a few kilobytes beside its clip, so this really bounds how many clips replay instantly |

Fetching a Sync Module clip means asking the module to upload it to Blink's
cloud and waiting for it to land, several seconds each, so the proxy does one
at a time and never the same one twice. Thumbnails are only requested for the
rows on screen.

## Proxy Token

Both install paths provision this, so there is normally nothing to do:

| Install | Where the token comes from | Where it lives |
|---|---|---|
| `scripts/install-proxy.sh` | Generated on first install, never rotated after | `/etc/blink-liveview-proxy/blink-liveview-proxy.env` |
| Add-on | Generated on first start unless `proxy_api_token` is set | `/data/proxy-token`, shared with the integration |

To set one yourself on a manual install, the service reads it from the
environment:

```bash
export BLINK_PROXY_TOKEN="long-random-token"
```

Enter the same token in the HA integration config flow — the add-on pre-fills
it. If a token is ever rejected, Home Assistant opens a reauthentication prompt
for that entry rather than failing silently.

The token is also what gates browser authentication. `/auth/*` answers `503`
while it is empty, and accepts it only as an `Authorization: Bearer` header —
never as `?token=`. Keep the token out of dashboards, shell history, and URLs;
Home Assistant adds it server-side for the authentication panel, so no browser
ever receives it.

## Install and Update Knobs

`scripts/bootstrap.sh` and `scripts/install-proxy.sh` read these from the
environment:

| Variable | Default | What it does |
|---|---|---|
| `VERSION` | newest stable or prerelease tag | Install a specific tag instead |
| `FORCE` | unset | Reinstall even when that tag is already installed |
| `SRC_DIR` | `/opt/src/ha-blink-live-view-proxy` | Where the checkout lives |
| `BIND_HOST` | `0.0.0.0` | `127.0.0.1` keeps the proxy on loopback |
| `PROXY_PORT` | `8088` | Listen port written into a new config |
| `BLINK_PROXY_TOKEN` | generated | Use a token of your own |
| `INSTALL_DEPS` | `1` | `0` skips the apt prerequisite install |
| `INSTALL_WATCHDOG` | `1` | `0` skips the stuck-refresh watchdog |
| `INSTALL_AUTOUPDATE` | `0` | `1` enables the nightly update timer; the on-demand updater is installed either way |

## Blink Account Authentication

Blink credentials belong to the proxy, not the integration. The integration
configures only the proxy URL, live-view duration, and this proxy token.

Three ways to authenticate, all documented step by step in
[OPERATIONS.md](OPERATIONS.md#re-authenticating):

| Where | How the PIN is delivered |
|---|---|
| **Blink Proxy → Authentication** tab | Typed into the page that started the login |
| Add-on without a browser | `blink_2fa_code` option, saved while it runs |
| Linux CLI | Answered at the interactive `Blink 2FA code:` prompt |

In all three the PIN is only issued *after* sign-in begins and is only valid for
the process that asked for it, so a start-time value in `--pin` or
`BLINK_2FA_CODE` (the `twofa_env` field) is deliberately ignored with a warning.
The `auth_file` holds the resulting refresh data; back it up like a secret and
keep it out of git.
