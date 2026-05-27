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
  "mpegts_session_seconds": 60,
  "mpegts_cooldown_seconds": 30,
  "ptt_disabled_camera_types": ["mini"],
  "ptt_disabled_product_types": ["owl"],
  "cameras": {}
}
```

## Camera Map

The proxy can discover Blink cameras without a camera map. A map is still useful
for stable slugs and for linking proxy cameras back to official Home Assistant
Blink camera entities.

Use `name`, `id`, or `serial` to match a Blink camera:

```json
{
  "cameras": {
    "front_door": {
      "name": "Front Door",
      "entity_id": "camera.front_door"
    }
  }
}
```

`entity_id` should point to the official HA Blink camera entity. That lets the
custom integration use the normal snapshot in loading screens and snapshot
refresh actions.

## Live View Duration

The Home Assistant integration has an options flow:

```text
Settings > Devices & services > Blink Liveview Proxy > Configure
```

`Live-view duration in seconds` controls how long the direct player asks the HA
route/proxy to keep each live view open. Valid range: `10-300` seconds.

Blink can still end sessions early.

## Push-to-Talk

Browser microphone capture requires HTTPS or a browser-trusted origin. The
player sends PCM to HA over WebSocket; HA forwards it to the proxy; the proxy
uses ffmpeg to encode AAC and sends IMMI audio frames to Blink.

PTT is hidden for camera families in:

```json
"ptt_disabled_camera_types": ["mini"],
"ptt_disabled_product_types": ["owl"]
```

## Local Clips

The HA clip viewer intentionally uses local Sync Module clips:

```text
/api/blink_liveview_proxy/clips/viewer
```

The proxy also has diagnostic support for cloud clips, but the HA viewer does
not expose them.

## Proxy Token

If the proxy listens anywhere broader than `127.0.0.1`, set:

```bash
export BLINK_PROXY_TOKEN="long-random-token"
```

Enter the same token in the HA integration config flow.
