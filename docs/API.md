# Proxy API

These are the local proxy endpoints used by the Home Assistant custom
integration and direct player. Keep the proxy bound to `127.0.0.1` unless you
also configure `BLINK_PROXY_TOKEN`.

The route handlers live in
`proxy/blink_proxy/routes.py`; protocol-specific push-to-talk handling lives in
`proxy/blink_proxy/ptt.py`. That split is intentional so Blink endpoint changes
can usually be patched without touching CLI, install, or HACS packaging code.

## Health and Inventory

- `GET /health`
- `GET /cameras`
- `GET /`

## Live View

- `GET /cameras/{slug}/mpegts`
- `GET /cameras/{slug}/hls/index.m3u8`
- `GET /cameras/{slug}/hls/{filename}`

Useful `mpegts` query parameters:

- `seconds`: maximum session length requested by the direct player
- `session`: browser session ID used by push-to-talk
- `force=1`: bypass local cooldown after a previous live view

## Push-to-Talk

- `GET /cameras/{slug}/ptt`

This is a WebSocket endpoint. The browser sends start/stop JSON messages and
binary signed 16-bit PCM chunks. The proxy encodes AAC with `ffmpeg` and sends
Blink IMMI audio frames over the active live-view session.

## Last Watched Live View

- `GET /cameras/{slug}/last-liveview`
- `GET /cameras/{slug}/last-liveview.ts`
- `GET /cameras/{slug}/last-liveview.mp4`

The MP4 endpoint remuxes the cached MPEG-TS file with `ffmpeg` on demand.

## Local Clips

- `GET /clips?source=local&hours=24&limit=20`
- `GET /clips/{clip_id}.mp4?source=local`

The Home Assistant viewer intentionally uses local Sync Module clips. Cloud clip
support remains a diagnostic proxy path and is not surfaced in the HA viewer.
