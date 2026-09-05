# Changelog

What changed in each tagged release, and why. The full notes — including
upgrade warnings and credits — are on the
[releases page](https://github.com/Teethree89/ha-blink-live-view-proxy/releases).

While this is pre-1.0, the minor version moves for anything user-visible (new
behaviour, a dropped architecture, a changed default) and the patch version for
fixes that change nothing about how it is used.

## [0.7.0-rc.3] — 2026-09-04

- **An add-on install is offered an address that actually reaches the add-on.**
  The setup form pre-filled `http://homeassistant.local:8088`, a host address —
  and the add-on publishes no host port unless someone maps `8088` by hand, so
  the one path documented as a single click was the one path that could not
  work: `cannot_connect` on the first screen, with a healthy proxy sitting
  behind it ([#30](https://github.com/Teethree89/ha-blink-live-view-proxy/issues/30)).
  The add-on now writes the address it is reachable on next to the token it
  already shared — its own hostname on Home Assistant's internal network, and
  the port this start is really listening on. The config flow prefers that,
  falls back to deriving the hostname from Supervisor's add-on inventory (so an
  older add-on is fixed by updating the integration alone), and only then tries
  `homeassistant.local`. Each candidate is probed before it is offered, so the
  address in the form is one that answered. Nothing needs a published port:
  every request to the proxy is made by Home Assistant, never by the browser.

## [0.7.0-rc.2] — 2026-09-04

- **Prereleases now identify and update themselves as prereleases.** The HACS
  integration, Supervisor add-on and proxy all report `0.7.0-rc.2`, so the
  dashboard no longer calls an RC the final `0.7.0`. Version comparisons order
  RC iterations correctly, the host updater considers both stable and
  prerelease tags without ever automatically downgrading, and tagged RC
  container images publish under their version without replacing `latest`.

## [0.7.0-rc.1] — 2026-09-04

Release preparation ahead of 1.0: the product gets its proper name, the clip
viewer gets thumbnails and a layout that holds still, phones get a live view
that fits the screen, and the three things standing between this and a
default HACS listing are gone.

- **It is called Blink Live View Proxy now, everywhere you can read it.** The
  wordmark always said so; the manifest, HACS, the add-on store, the config
  flow, the sidebar and the docs said "Liveview". Nothing that code depends
  on moved: the domain is still `blink_liveview_proxy`, every URL and entity
  id is unchanged, and the health sensor keeps
  `binary_sensor.blink_liveview_proxy` on a fresh install too — it names its
  own object id rather than letting the new name derive a second one.
- **The sidebar entry carries the logo.** A `blink:` icon set ships with the
  integration and is loaded on every page the way HACS loads its own, so the
  one-colour mark from the wordmark is what the sidebar draws, and
  `blink:logo` works anywhere an icon name does. The generated dashboards use
  it on the proxy pill, in both states: the pill used to swap to
  `mdi:cctv-off` when the proxy was down, which is not a variant of the mark
  but a different object, so the tile stopped looking like this integration
  exactly when someone was reading it. Colour carries the state instead, which
  is only safe because `show_state` prints the word underneath. A slashed
  variant of the mark was drawn and rejected: with no knocked-out gap the
  slash merges into the rings and is unreadable at the 24 and 40px the sidebar
  and the pill actually draw.
- **The add-on store entry says it is unofficial.** The README said so twice
  and the add-on description did not, which is the one place someone sees this
  next to Amazon's own listings with no other context.
- **Every string in the config flow, the options flow and the three repair
  issues rendered as its raw key.** `strings.json` is a build-time file that
  Home Assistant compiles for core integrations and reads from nowhere at
  runtime; a custom integration has to ship `translations/en.json`, and this
  one never had. It does now, and a test keeps the two identical.
- **The Home Assistant floor is 2024.11.0, which is what it always needed.**
  The options flow reads `OptionsFlow.config_entry`, a property that appeared
  in 2024.11. `hacs.json` promised 2024.6.0, so on the four releases between
  the integration installed cleanly and raised the moment Options opened.
  A test now holds `hacs.json` and the panel's own floor to one number.
- **CI no longer ignores the brands check, because it passes.** The icon
  lives in `brand/` beside the manifest, which is what the check looks for
  first since Home Assistant 2026.3 stopped taking custom icons in its brands
  repository. An ignore anywhere disqualifies a HACS default submission, and
  this was the last one.
- **Clips have thumbnails.** Each row in the clip viewer shows the clip's
  first frame, with a spinner until it arrives. The proxy cuts it with ffmpeg
  from a copy of the clip it now keeps: a Sync Module clip cannot be read in
  part — blinkpy has the module upload the whole thing to Blink's cloud and
  polls until it lands — so each clip is fetched exactly once, one at a time,
  and kept under a 512 MB cap (`clip_cache_dir`, `clip_cache_max_mb`; the
  add-on and the image use `/data/clips`, a systemd install lands beside its
  live-view cache). Thumbnails load only for the rows on screen. A proxy
  older than 0.7.0 lists clips without them and the row shows a placeholder.
- **Preview and Download no longer go to Blink every time,** and the player
  can seek: a cached clip is served as a file, which answers byte ranges,
  and Home Assistant forwards them. That is also what Safari requires before
  it will play an MP4 at all — on an iPhone, Preview used to do nothing.
- **The clip viewer holds still.** The page grew with the list, so the
  preview stretched to the height of sixty rows with the video somewhere in
  the middle. It is two panes now: the list scrolls on its own, the player
  is locked to the viewport beside it and centred, and on a phone the video
  sits on top with the list underneath. Arrow keys move through the list.
- **Live view fits a phone in portrait.** The dialog was sized in `100vh`,
  which on a phone is the height with the browser toolbar hidden, so the
  bottom of the video and its controls sat behind the toolbar until the
  phone was turned sideways. It uses the visible height now, and keeps the
  header and controls clear of the notch and the home bar.
- **Generated dashboards stack on a phone.** Whole-dashboard and view output,
  from the YAML tab and the generator alike, is a sections view with up to
  three tiles across on a desktop and one column on a phone; the flat card
  list before it squeezed a tile and its four buttons into half a phone
  screen. The buttons are 40px, Home Assistant's own touch size, up from 36.
- **The panel says what to do before the integration is set up.** It is in
  the sidebar from the first restart after installing, which is exactly when
  someone needs the install steps, and a 503 was the one thing it showed.
  Overview now lays out the three ways to run the proxy, offers to start
  the config flow, and runs the checks that do not need a proxy.

Found by testing it on a real install, and fixed here:

- **The phone's safe areas were white around a live view.** The full-screen
  shell took the notch and home-indicator insets as padding, and that padding
  exposed Home Assistant's light-theme card background: below the picture in
  portrait, and beside and below it in landscape. The player now paints through
  those insets while the floating controls carry their own safe-area offsets.
  In landscape, the close button measures the letterboxed video and sits just
  outside its left edge instead of overlapping the picture. In portrait, Hold
  Talk and End move into the empty space below the video when that gutter is
  deep enough to clear both the native controls and the home indicator.
- **The sidebar icon was blank until a hard refresh** — and on the iOS
  companion app there is no way to force one. `add_extra_js_url` puts the icon
  set in `index.html`, which is right for a fresh page load and useless to a
  tab that was already open when the integration was installed: Home Assistant
  renames the sidebar entry over the websocket, so it looks updated, while the
  script tag never arrives. The set is now loaded from the dialog resource and
  the panel as well, and once it registers it repaints any `blink:` icon that
  already gave up — `ha-icon` resolves a custom prefix once and never asks
  again, so re-assigning the name is what makes it look a second time.
- **A burst of thumbnails could come back 502, and stayed broken.** Every
  visible row asked at once, all of it queued behind the proxy's one-at-a-time
  Blink fetch, the last waited most of a minute, and Blink started throttling
  the run of `prepare_download` calls. The viewer now keeps its own two-deep
  queue so rows fill top-down, and retries a failed thumbnail twice before
  showing a placeholder. A proxy that is merely still signing in answers 503
  now instead of being collapsed into a 502 — the difference between "try
  again" and "the gateway is broken".
- **The clip rows are their own placeholder now, not a spinner.** There is no
  `<img>` in a row any more: the picture arrives as a background on a div of
  fixed size, and a background image takes no part in layout, so a row cannot
  change height when it loads. With an img the tile took its height from the
  one child whose size is unknown until the network answers, and a list of
  cold rows was a stack of squat rows that each jumped to full height as its
  own picture landed. A shimmer runs over the tile until then. The spinner all
  this replaced was 22px on a 132px tile, moving between two colours 1.21:1
  apart — present, correct, and invisible, which for a loading state is the
  whole failure.
- **Frontend files carry the version in their URL now**, the way HACS puts
  `?hacstag=` on everything it registers — the dialog resource, the panel
  module and the icon set. They were served `no-cache` with an ETag, which
  makes a *browser* revalidate and is not the layer that matters: once a
  document has imported a module it stays in that document's registry for as
  long as the document lives, and the companion app keeps its webview alive
  across app switches. So an upgrade could leave old frontend code resident
  with no way to shift it but quitting the app. A new version is a new URL,
  which is a different module, so it cannot happen again. An entry from an
  older version — or from before the version existed — is moved to the
  current one rather than duplicated.
- **The dialog has no header any more, just a floating close button.** It was
  a fixed 56px of every screen, spent on a camera name the viewer had tapped
  a moment earlier and can see in the picture. On a phone in landscape that
  was most of the height the video needed, and the bottom of the picture —
  along with iOS's native controls — fell off the screen because of it. The
  name survives as the dialog's accessible name and in the player's own
  loading panel; the button costs nothing and the safe-area insets it used to
  absorb are now the shell's, which in landscape are zero exactly where the
  height was wanted.
- **The live-view dialog was taller than an iPhone's screen.** `100dvh` is
  still not what the companion app's webview actually shows, so the bottom of
  the shell — where iOS puts the native video controls, AirPlay included — sat
  below the fold. The dialog now takes its height from `window.innerHeight`
  and follows it on resize, rotation and Safari's sliding toolbars. On a phone
  the video element also shrink-wraps the picture rather than filling all that
  black, so those controls sit under the video where they belong. That has to
  be done with flex: the stage is a grid whose only row carries no explicit
  size, and a `max-height: 100%` measured against a row that grows to fit its
  own content clamps nothing — which cropped the bottom off a landscape live
  view. A flex container has a definite height here, so the picture is
  letterboxed instead of overflowing.

- **"End & Save" is now just "End".** It was doing three things — stop the
  stream, wait for its recording to be finalized, download it — and the
  waiting could not work on the HLS path: a session is only finalized when it
  stops, and it did not stop until the idle timeout, which is three quarters
  of a minute on a tuned install. The poll gave up in seven seconds and
  reported a failure for a recording that did arrive, forty seconds later.
  Ending and saving are separate now: **End** stops the stream, and the
  **Save MP4** and **Start Again** buttons that already existed take it from
  there. All the polling is deleted.
- **A live view now ends when you close it, not up to a minute later.** The
  player asks the proxy to end the session — on closing the dialog as well as
  on End — where before only the MPEG-TS path stopped promptly, because its
  stream ends with the connection. On HLS the camera went on streaming to
  nobody for the whole idle timeout, which on a battery camera is battery.
  Every path a live view can finish by goes through the same call, so the
  recording is always finalized before Save MP4 is offered.
- **"End & Save" never worked on an iPhone, and "Save MP4" there was lying.**
  The cached copy of a live view was written only by the MPEG-TS handler, and
  iOS has no Media Source Extensions so it always takes the HLS path, which
  wrote none. End & Save waited for a recording that was never going to
  appear and gave up; Save MP4 then handed back whatever older session
  happened to still be in the cache — the wrong clip, with nothing to say so.
  The HLS session now writes the same cached copy as a second ffmpeg output,
  always a straight copy even when low-latency mode is re-encoding the
  playlist, finalized only once ffmpeg has actually written something.
- **The live-view buttons are centred on a phone.** Hold Talk and End & Save
  sat in the top-right corner, on top of the native mute and AirPlay controls
  now that the picture reaches the edges.

Two halves again: the thumbnails need proxy 0.7.0. The integration's repair
notice and the Overview tab say so and offer the update where the install
supports it.

## [0.6.2] — 2026-09-03

Four ways the frontend could silently do nothing, and the update button now
shows its work.

- **Frontend files moved off `/static/`, because Home Assistant was caching
  them forever.** Its service worker registers a `CacheFirst` route for
  `/(static|frontend_latest|frontend_es5)/.+` *before* its `/api` rule, and
  Workbox matches a regular expression anywhere in a same-origin URL rather
  than only at the start — so `/api/blink_liveview_proxy/static/...` matched.
  The browser served those files out of Cache Storage without ever asking the
  server again, and `ignoreSearch` on that route means a `?v=` cache-buster is
  stripped from the key and cannot help. Only the path could move, so it did:
  `/api/blink_liveview_proxy/assets/...`. This only ever affected HTTPS, since
  a service worker needs a secure context — which is exactly why it survived
  so long, because over plain HTTP everything looked correct.
- **The old path is still served.** Every dashboard, YAML resource list and
  hand-written config in the wild points at it, and a 404 there is the silent
  dead dashboard all of this exists to prevent. Where Lovelace can be written
  to, the registered resource is rewritten in place rather than duplicated.
- **The panel loads the dialog helper itself.** Home Assistant loads Lovelace
  resources on a Lovelace dashboard and nowhere else, so a session that opened
  the sidebar panel directly had nothing listening for the events the Cameras
  tab fires — every Live view, Clips and Refresh snapshot button in the panel
  did nothing at all. The helper guards itself, so a dashboard that already
  loaded it is unaffected.
- **Resource registration retries instead of giving up once.** It ran during
  config-entry setup and returned silently when Lovelace had not started yet;
  `after_dependencies` makes that ordering usual but not certain, so a boot
  that lost the race registered nothing and said so only at debug level. It
  now waits for Home Assistant to finish starting and tries again, and says so
  at warning level when it finally cannot.
- **A started proxy update shows a progress bar.** It follows the restart
  through health and the reported version, offers a reload when the new
  version arrives rather than forcing one, and gives up after five minutes
  with a pointer to the logs — an update that fails leaves the proxy on its
  old version and says nothing, so a bar that span forever would read as
  progress.
- **Overview reports whether the integration itself is up to date**, read from
  the update entity HACS already publishes — no network call, no rate limit,
  and it honours the release channel configured in HACS. An integration copied
  into `custom_components/` by hand has no such entity, and the row says so
  rather than guessing.

## [0.6.1] — 2026-09-03

Nothing about how the proxy is used changes. The dashboard says more about the
install it is running on.

- **Overview checks the prerequisites and says what to do about each one.**
  Seven rows: the Home Assistant version, the official Blink integration, the
  proxy's blinkpy and ffmpeg, the Lovelace dialog resource, and the two HACS
  frontend cards. The three that matter most fail silently today — a missing
  dialog resource makes every tile inert with no console error, no log line and
  no failed request; a blinkpy a few releases back reports a failed login while
  Blink texts the code anyway; a missing official Blink integration shows up
  only as one button returning a 404.
- **The setup steps are attached to each check, not to its failure.** Every row
  carries the same accordion whether it passes or not, because the reference
  for rebuilding this on a new host is worth having before anything breaks.
  Unmet checks open theirs on the first paint, and a background poll no longer
  closes one that is being read.
- **A check that cannot be answered says so instead of guessing.** A proxy
  older than 0.6.1 does not report its environment and Lovelace may not have
  started yet; both are ordinary states of a working install, and calling them
  failures would send people to repair something already correct.
- **The proxy reports its Python, blinkpy and resolved ffmpeg path on
  `/status`.** Behind the same token as the version field, for the same reason:
  library versions and binary paths are what someone probing the host wants.
- **The dialog resource is registered on every supported Home Assistant, not
  just the newest.** `hass.data["lovelace"]` was a plain dict up to 2025.1, a
  dataclass with `mode` from 2025.2, and only from 2026.3 does it carry
  `resource_mode` — which is what the registration read. Below 2026.3 it found
  nothing, concluded YAML mode and returned, so the resource was never added
  and every live view, clips and snapshot button stayed silently inert. All
  three shapes are now read, and each is covered by a test.
- **The dashboard wears the wordmark**, and the direct player and clip viewer
  have a browser-tab icon. Home Assistant only serves an integration's own
  `brand/` images from 2026.3.0, and the floor here is 2024.6.0, so the
  integration serves them itself. The header picks the light or dark wordmark
  from the theme's actual background rather than `prefers-color-scheme`: a
  light OS runs a dark theme perfectly happily, and the navy wordmark measures
  1.28:1 there.

## [0.6.0] — 2026-09-03

- **Updates can be started from Home Assistant.** When the integration is
  newer than the proxy, Repairs offers a confirmation-gated Fix button. A
  systemd install starts its separately installed updater unit; an add-on asks
  Supervisor to update it. Standalone containers and hand-built installs stay
  manual, and unattended timer updates remain opt-in.
- **The sidebar entry is now a full admin dashboard.** Overview shows proxy
  health, integration/proxy versions, and an update action when one is needed.
  Cameras & entities shows the discovered inventory, model/serial/network
  details, live view, clips, snapshot refresh, push-to-talk availability, and
  links to every related native Home Assistant entity. The existing secure
  browser login is retained as the Authentication tab.
- **Dashboard YAML can be generated in the browser.** The YAML tab produces a
  complete dashboard, one view, or a paste-ready card for all cameras or one
  selected camera, with a clipboard action and no proxy token in its output.
- **An open direct player can recover after its short-lived browser token
  expires.** A Home Assistant-authenticated refresh route mints a new scoped
  token; expired or rotated-out tokens cannot mint their own replacements.

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
