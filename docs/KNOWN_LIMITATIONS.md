# Known Limitations

- This is built on BlinkPy and observed Blink app behavior, not an official
  Amazon/Blink API contract.
- This project is unaffiliated with Amazon or Blink and is intended for
  interoperability with Blink cameras you own.
- Blink can change authentication, cloud endpoints, IMMI framing, or app
  behavior at any time. Live view and push-to-talk are more likely to break than
  normal snapshots or motion controls.
- The repository should not include Blink app binaries, copied app code,
  captures, account tokens, client secrets, or Amazon/Blink brand assets.
- The HA custom integration does not perform Blink login. The proxy owns Blink
  auth and stores the refresh token. The integration's **Blink Authentication**
  panel is a front end for the proxy's authentication routes; it needs a proxy
  API token configured on both sides, and it is restricted to Home Assistant
  administrators.
- Blink 2FA cannot be automated. A PIN is only issued after sign-in starts, it
  is valid only for the process that asked for it, and it must be entered while
  that process is still running. Restarting the proxy, add-on, or CLI between
  the two steps always forces a new login and a new PIN.
- Only one Blink login attempt can be in flight at a time. A second attempt is
  rejected while one is active, and an unanswered challenge expires after
  fifteen minutes — Blink's own PIN expires well before that.
- The proxy serves its HTTP API before Blink login finishes. Camera, live-view,
  and clip routes answer `503` until a session exists, and `serve` does not
  prompt for a PIN on a terminal — use the panel, the add-on option, or the
  `list` command.
- A successful reauthentication replaces the Blink session, which ends any live
  view that happens to be open at that moment.
- The Docker image is built for `linux/amd64` and `linux/arm64` only, matching
  the add-on. 32-bit ARM cannot usefully transcode a live stream.
- Motion zones, camera settings, and deep account administration are not
  implemented.
- Push-to-talk is experimental. Tested regular Blink cameras can receive audio.
  Blink Mini/`owl` cameras remain disabled by default, but specific slugs can be
  force-enabled with `ptt_force_enabled_slugs`; one was confirmed audible that
  way on June 30, 2026.
- On low-power Android wall panels, tap-to-toggle talk is more reliable than
  press-and-hold because WebView is decoding video and capturing microphone
  audio at the same time.
- Live view wakes cameras and consumes Blink live-view/cloud quota.
- The direct player downloads the most recent watched live view; it is not a
  general DVR.
- Local clips depend on a Sync Module with local storage and BlinkPy's local
  storage manifest support.
- Cloud clips are kept as a proxy diagnostic path and intentionally skipped in
  the HA viewer.
- The dashboard helper expects `custom:button-card` or another card that can
  fire `fire-dom-event` actions.
