# Known Limitations

- This is built on BlinkPy and observed Blink app behavior, not an official
  Amazon/Blink API contract.
- The HA custom integration does not perform Blink login. The proxy owns Blink
  auth and stores the refresh token.
- Motion zones, camera settings, and deep account administration are not
  implemented.
- Push-to-talk is experimental. Tested regular Blink cameras can receive audio;
  Blink Mini/`owl` cameras are disabled by default because they dropped live
  sessions during audio injection.
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
