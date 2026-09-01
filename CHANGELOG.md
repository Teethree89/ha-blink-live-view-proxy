# Changelog

What changed in each tagged release, and why. The full notes — including
upgrade warnings and credits — are on the
[releases page](https://github.com/Teethree89/ha-blink-live-view-proxy/releases).

While this is pre-1.0, the minor version moves for anything user-visible (new
behaviour, a dropped architecture, a changed default) and the patch version for
fixes that change nothing about how it is used.

## [0.3.0] — 2026-09-01

Browser authentication, and setup that provisions itself.

- **Blink Authentication panel.** An admin-only Home Assistant panel signs in to
  Blink and takes the 2FA PIN in the same live session, so nothing has to be
  restarted mid-login. It shows idle, authenticating, waiting-for-PIN, success,
  expired and failure, supports deliberate reauthentication, and refuses a
  second attempt while one is active.
- **The proxy serves before it logs in.** `/health`, `/status` and `/auth/*`
  answer while authentication is pending; camera, live-view and clip routes
  answer `503` until a session exists. A login that needs a human no longer
  exits the process, which removes the restart cycle that could text a code each
  time.
- **Access control for the credential routes.** `/auth/*` requires the proxy
  token as an `Authorization: Bearer` header, rejects it in a query string, and
  refuses to run at all when no token is configured. The Home Assistant routes
  require an authenticated administrator and add the token server-side, so the
  browser never holds it. No credential reaches a URL, log, response, asset or
  browser storage.
- **`/status` gained `auth_state`,** so a login stuck waiting on a human is
  visible instead of inferred from cameras going quiet.
- **Unattended install.** `scripts/install-proxy.sh` installs missing
  prerequisites, writes a config that discovers cameras, generates a proxy API
  token, starts the service and waits for `/health`. Re-running it upgrades in
  place and leaves the token, config and Blink session alone.
- **The add-on provisions its own token** when `proxy_api_token` is empty, keeps
  it across updates, and shares it with the integration, whose setup form
  arrives pre-filled. A rejected token now opens a reauthentication prompt
  rather than failing the config entry.
- **Every user-facing document was corrected.** A PIN only exists after sign-in
  begins and dies with its session, so the restart-after-PIN and
  pre-supplied-PIN instructions are gone.
- Fixed: token refreshes after a browser login now reach the auth cache; a
  non-ASCII `Authorization` header returns `401` instead of `500`; the panel no
  longer clears a PIN being typed; the challenge timeout matches the add-on's
  fifteen-minute wait.
- The CLI prompt and the add-on's option polling are unchanged, and covered by
  tests so they stay that way.

Upgrading: add-on users moving from a tokenless setup are asked once to confirm
the generated token, pre-filled. Fresh Linux installs bind `0.0.0.0` by default
now that a token is always provisioned — `BIND_HOST=127.0.0.1` keeps loopback,
and existing configs are never rewritten. `serve` no longer prompts for a PIN on
a terminal; use the panel, the add-on option, or the `list` command.

## [0.2.0] — 2026-09-01

Three silent failures made audible.

- **The dashboard resource registers itself.** Nothing registered
  `blink-liveview-dialog.js` as a Lovelace resource, so tiles fired their event
  into a void — no console error, no log line, no failed request. Storage-mode
  dashboards get it automatically now; YAML mode is told what to add.
- **A failed HLS start says why.** ffmpeg's stderr went to `DEVNULL`, leaving
  `ffmpeg exited with 1` for a dead camera, an unparseable stream, a dead URL
  and an unsupported codec alike. Both failure paths now log the tail, and say
  so when stderr was empty.
- **Dropped `armhf` and `armv7`.** Supervisor deprecated both, and that hardware
  cannot usefully transcode a live stream. Stay on v0.1.0 if you are on 32-bit
  ARM.
- Tests run in CI, and nothing in CI contacts Blink.

## [0.1.0] — 2026-09-01

First tagged release, so installs can pin instead of tracking the default
branch.

- Live view through a direct MSE player, with native HLS where MSE is
  unavailable — the only way an iPhone plays this stream.
- Push-to-talk on tested cameras and doorbells; Mini/`owl` disabled by default.
- Local Sync Module clip browsing, snapshot refresh, and End & Save.
- `GET /status` for readiness, camera counts, uptime and watchdog state, plus an
  optional systemd watchdog that backs off rather than hammering login.
- Three dashboard options: one hand-edited camera, a generator, or a
  self-populating view.
- **Login works for new users.** blinkpy pinned to 0.25.9 (0.25.5 read Blink's
  202 challenge as a failure while the text arrived anyway), the process stays
  in the open OAuth session to wait for the code, a code predating the challenge
  is never submitted, and a non-UUID `hardware_id` is discarded because Blink
  answers those with a bare 406 that reads as a wrong password.

[0.3.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.3.0
[0.2.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.2.0
[0.1.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.1.0
