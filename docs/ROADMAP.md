# Roadmap and known rough edges

Things worth doing, and things that are odd but deliberate. Not a promise of
dates — a list so nobody has to rediscover them.

## Known rough edges

### The proxy source exists twice

```
proxy/blink_proxy/          the Linux service
addon/proxy/blink_proxy/    bundled into the add-on image
```

They have to stay byte-identical, and that is maintained by hand. A change
landing in one copy and not the other is an easy mistake — it has already
happened once, in a pull request that added RTSP support to the add-on only,
leaving service installs unable to use those cameras.

CI now asserts the two trees match, so a divergence fails the build rather
than a user's install. The better fix is still open: one copy, and a build step
that vendors it into the add-on image.

### `/status` reports token expiry that reads as broken

`token_seconds_remaining` goes negative on a perfectly healthy proxy. BlinkPy
refreshes lazily — inside the request that needs the token — so an idle proxy
sits with an expired token until the next live view, then renews on demand.

The number is accurate. It is just not the question anyone is asking, and it
invites alerting on the wrong thing. What matters is whether a *refresh* can
still succeed, which is different.

### Nothing signals that re-auth is needed

Two states look identical from outside:

- the refresh token still works — silent self-heal, no human needed
- the refresh token is rejected — `Auth.startup()` falls through to a full
  login flow at `debug` level, sends an SMS, and needs a human

The first sign of the second is usually cameras not working, or an unexplained
text. `/status` should carry an `auth_state` (`ok` / `refresh_due` /
`reauth_required`) so a dashboard can say so once, instead of the user finding
out. The package example currently approximates it from the watchdog counter.

### Blink cannot be tested automatically

Nothing in CI contacts Blink, and nothing should start to: every run would be a
login attempt, and repeated attempts get the account rate-limited and text the
owner each time.

That leaves the protocol paths untested. Two ways forward, in order of value:

1. **Fixtures.** Capture real IMMI and RTSP bytes once, commit them, and assert
   the parsers handle them. Tests real data, nothing to drift.
2. **A proxy stub.** The integration only calls `/health`, `/cameras` and
   `/cameras/{slug}/mpegts`, so a small fake server would let the integration
   and dashboards be tested end to end without a proxy or an account.

Faking Blink's cloud itself is explicitly *not* wanted. It would mean
reimplementing their server from our own assumptions, and the tests would then
only prove the stub agrees with the code — staying green while production
breaks.

## In flight

- **RTSP transport** for cameras handed an `rtsps://` URL rather than
  `immis://` — older `xt` and `white` models, which have no live view at all
  today. See the open pull request.

## Wanted

- **A dashboard card**, so the tiles do not have to be assembled from
  `button-card` and `fire-dom-event`. The generator exists because that
  assembly is fiddly; a real card would remove the need for it.
- **Motion zones and camera settings**, currently out of scope.
- **Cloud clip browsing.** Deliberately not surfaced — the local Sync Module
  path is the supported one — but the proxy has a diagnostic route for it.

## Deliberately not doing

- **Logging in to Blink from the integration.** It only ever talks to the
  proxy. That is what makes it safe to run a second Home Assistant against the
  same proxy for testing, and what keeps credentials in one place.
- **Retrying a failed login automatically.** Blink throttles hard — five
  attempts in 55 seconds and everything after fails, with a text each time.
  Every retry path here has a cap and a cooldown for that reason.
