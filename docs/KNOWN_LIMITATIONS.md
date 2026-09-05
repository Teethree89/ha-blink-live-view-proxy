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
  auth and stores the refresh token. The integration's **Blink Proxy → Authentication**
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
- **Push-to-talk needs an HTTPS address, and fails invisibly without one.**
  Browsers only expose a microphone in a secure context, so Hold Talk cannot
  work over `http://<address>:8123` — Home Assistant's own default — however
  the proxy itself is reached. The button is enabled from whether the *camera*
  supports it, so on plain HTTP it is offered, looks live, and does nothing:
  the refusal is written to a status line the player has already hidden by the
  time the button becomes usable, and nothing is logged proxy-side because
  nothing was ever sent. The panel's Overview now has a row that says which
  kind of address you are on. Reported by @fritzzetik, reproduced by
  @bbolinger; making the button disable and label itself is the fix still to
  come.
- Push-to-talk is experimental. Tested regular Blink cameras can receive audio,
  Blink Mini/`owl` included — one was confirmed audible on June 30, 2026, and
  again on September 5, 2026, which is why the family is no longer denied by
  default.
- Push-to-talk is not available at all on cameras Blink serves over the RTSP
  transport (the older `xt` and `white` models). That transport carries no
  upstream audio channel, so the proxy raises `push-to-talk is not available
  over RTSP`. Live view on those cameras is otherwise unaffected. They are on
  `ptt_disabled_product_types` by default so the button is never offered there.
- Push-to-talk does not work on the Wired Floodlight (`superior`) either, for a
  different reason: it gets Blink's IMMI transport, so the path exists, but the
  audio shape the camera expects is not the one the proxy sends. Measured cost
  of pressing it there is worse than a refusal — the camera closes the stream
  mid-hold and will not rejoin a live view for about three minutes — so it is
  denied by default too. A capture of what Blink's own app sends to a
  `superior` would make this fixable, and the entry should come out then.
  Measured by @bbolinger.
- Which transport a camera gets is Blink's decision, not a setting. A model
  moved from `rtsps://` to `immis://` by Blink would gain push-to-talk, and a
  model moved the other way would lose it, with no change here.
- On low-power Android wall panels, tap-to-toggle talk is more reliable than
  press-and-hold because WebView is decoding video and capturing microphone
  audio at the same time.
- Live view wakes cameras and consumes Blink live-view/cloud quota.
- The direct player downloads the most recent watched live view; it is not a
  general DVR.
- Local clips depend on a Sync Module with local storage and BlinkPy's local
  storage manifest support.
- Cloud clips are listed in the HA viewer, but they cost more to show than
  local ones: a thumbnail is the first frame of the clip, so drawing one means
  downloading the whole clip from Blink. The newest six are fetched
  automatically, the rest wait until a clip is played or **Load cloud
  thumbnails** is pressed. Cloud clips also only exist for an account with a
  Blink subscription.
- The dashboard helper expects `custom:button-card` or another card that can
  fire `fire-dom-event` actions.
