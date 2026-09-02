# Changelog

What changed in each tagged release, and why. The full notes — including
upgrade warnings and credits — are on the
[releases page](https://github.com/Teethree89/ha-blink-live-view-proxy/releases).

While this is pre-1.0, the minor version moves for anything user-visible (new
behaviour, a dropped architecture, a changed default) and the patch version for
fixes that change nothing about how it is used.

## [0.5.1] — 2026-09-02

Icons, and only icons. Nothing about how the proxy is used changes.

- **The integration ships its own brand images.** `home-assistant/brands` no
  longer accepts custom integrations — a bot closes the PR — because since
  2026.3.0 an integration serves its own from a `brand/` folder next to
  `manifest.json`, which Home Assistant prefers over the CDN and serves at
  `/api/brands/integration/<domain>/<image>`. All eight slots are filled: icon
  and logo, each with an `@2x` and a dark variant. Home Assistant older than
  2026.3.0 keeps falling back to the CDN placeholder exactly as before, so the
  version floor is unchanged.
- **The dark variants are not decoration.** The navy wordmark measures 1.28:1
  against Home Assistant's dark background and the floor for large text is
  3.0:1, so it was very close to invisible there. The dark set swaps it for
  white at 17.04:1 and leaves the cyan alone.
- **The add-on has an icon and a logo**, 128×128 and 520×155, matching the
  official add-ons. Supervisor reads only those two filenames from the add-on
  folder and has no dark slot, so that one is the light wordmark.

## [0.5.0] — 2026-09-02

Live view for the cameras that never had it, and a much shorter wait for the
ones that did.

**Upgrade note: `hls_idle_timeout` now defaults to `10` seconds, down from
`45`.** This applies whether or not you opt into anything else below. An HLS
session is stopped that long after the last playlist or segment request, and a
playing client asks for something at least once per segment, so the change
should only ever shorten the gap between a viewer closing and the camera being
released. Set `hls_idle_timeout` back to `45` if a slow client ever loses its
session.

- **RTSP transport, so older `xt` and `white` cameras have live view at all.**
  Blink does not hand every camera an `immis://` URL; the oldest generations
  get `rtsps://`, and those were previously rejected outright. Handing that URL
  to ffmpeg does not work either, because Blink's RTSP server breaks RFC 2326
  three separate ways — every response carries `CSeq: 1` rather than echoing
  the request, and `SETUP` replies omit both `Session` and `Transport`. ffmpeg
  aborts on the first of those before the camera is ever asked to wake, so the
  camera never lights up and nothing says why. The proxy now performs the
  handshake itself and treats all three fields as optional. Push-to-talk is not
  available over this transport. Thanks to @fritzzetik, with review from
  @bbolinger.
- **A live view starts at the first keyframe instead of the third**, which was
  costing about eight seconds on every tap. `-fflags nobuffer` was the culprit
  and is gone: its only effect is at start-up, where it discards the packets
  ffmpeg read while working out what the stream is — and Blink's first keyframe
  is in those. The analysis window is bounded too (`ffmpeg_probesize`,
  `ffmpeg_analyzeduration`), because ffmpeg's MPEG-TS defaults wait five
  seconds for 5 MB that a sub-megabit stream never delivers. Thanks to
  @bbolinger.
- **An opt-in low-latency mode**, `hls_transcode` (add-on option
  `low_latency`), re-encodes video through `libx264 ultrafast` so keyframes can
  be forced every second and segments really are one second long, taking tap to
  picture from eight-to-twelve seconds down to two-to-seven. Off by default
  because it costs an encode per open live view — about a tenth of a core at
  720p on a desktop i5. It ships with a 2 Mbit/s ceiling and a pinned 24 fps
  output, both of which were needed to make real cameras work rather than being
  precautions. Thanks to @bbolinger.
- **A client waiting for a camera to wake now counts as active.** The idle
  reaper measured from a timestamp set once before `wait_ready()` began
  blocking, so any idle timeout shorter than `hls_start_timeout` would have
  stopped a session while its first request was still waiting on it. Invisible
  at the old 45s default; a prerequisite for the new one.
- Documentation: new **Live View Transports**, **ffmpeg Tuning**, **Low
  Latency** and **Session Lifetime** sections in `docs/CONFIGURATION.md`. The
  `ffmpeg_*` keys and both HLS timeouts had never been written down.

## [0.4.1] — 2026-09-01

Fixes the install one-liner, and a sweep of the things a release is a good
excuse to fix.

- **`curl … | sudo bash` aborted before it ran anything.** Piped into bash the
  script is read from stdin, where `BASH_SOURCE` does not exist, so the guard
  that keeps `source` from executing it tripped over `set -u` — in exactly the
  invocation the script exists for. The tests sourced the file, which is the one
  path where that variable is always set; they now also pipe it into bash the
  way the documentation does.
- **`/status` reports its version only to callers holding the proxy token.** The
  endpoint stays reachable without one, because dashboards and the watchdog use
  it, but a version number is a shopping list for whoever is asking. The
  integration sends the token, so its version check is unaffected.
- **The authentication panel stops polling every two seconds forever.** A live
  challenge still refreshes every two seconds — it has a countdown — but an idle
  or finished page drops to fifteen, and leaving the page stops it. Each tick
  was a round trip through Home Assistant to the proxy.
- Housekeeping: an unused import, an annotation naming a type its module never
  imported (invisible at runtime, wrong to every reader), and a mutable class
  attribute are gone, and `ruff` runs in CI with rules picked to catch mistakes
  rather than to enforce a style. It found the first two.

## [0.4.0] — 2026-09-01

Installing and updating the proxy, without a shell if you want one.

- **A standalone Docker image**, `ghcr.io/teethree89/ha-blink-live-view-proxy`,
  for hosts with neither Supervisor nor systemd — a NAS, a Docker-only box,
  Home Assistant Container on something that is not Debian. It writes its own
  config, discovers cameras, and generates its API token on first start; `/data`
  holds everything that must survive a container replacement. Built for amd64
  and arm64 on every push, published on a tag.
- **A one-line install that is also the upgrade.**
  `curl … bootstrap.sh | sudo bash` keeps a checkout on the proxy host, moves it
  to the newest **tag** — never `main` — and runs the installer. It exits with
  "nothing to do" when there is nothing to do, so it is safe to re-run.
- **Optional unattended updates.** `INSTALL_AUTOUPDATE=1` installs a nightly
  timer that runs that same check. Off by default: it restarts the camera proxy
  when it fires, which should be a decision rather than a surprise.
- **The authentication panel diagnoses failures** instead of blaming the URL and
  token. A proxy that predates `/auth`, one running without a token, a rejected
  token and an unreachable service now each get their own explanation and the
  command that fixes them, plus a **Check proxy** button to re-run the check.
  Home Assistant cannot apply those fixes — it has no shell on the proxy host —
  and the page says so rather than offering a button that does nothing.
- **A repair notice when the proxy is older than the integration needs.** The
  two halves update independently, so this is the common surprise after a HACS
  update; it now arrives as a notice naming the fix, and clears itself on the
  next poll after the upgrade. `/status` reports `version` for it, and a proxy
  that predates that field is placed by its capabilities so a correct install is
  never told to upgrade to what it already runs.
- Docs: the install guide is organised by which install you have, the upgrade
  procedure is written down — including that upgrading by copying files leaves
  the old blinkpy behind and breaks 2FA in a way that looks healthy — and this
  changelog exists.

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

[0.4.1]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.4.1
[0.4.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.4.0
[0.3.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.3.0
[0.2.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.2.0
[0.1.0]: https://github.com/Teethree89/ha-blink-live-view-proxy/releases/tag/v0.1.0
