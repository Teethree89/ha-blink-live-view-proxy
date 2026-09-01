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

### Half of "nothing signals that re-auth is needed" is fixed

Two states used to look identical from outside:

- the refresh token still works — silent self-heal, no human needed
- the refresh token is rejected — `Auth.startup()` falls through to a full
  login flow at `debug` level, sends an SMS, and needs a human

`/status` now carries `auth_state` (`idle`, `authenticating`,
`waiting_for_pin`, `success`, `expired`, `failure`), so a login that is stuck
waiting on a human is visible to a dashboard instead of being inferred from
cameras going quiet.

What is still missing is the *mid-life* case. BlinkPy refreshes lazily inside
the request that needs the token, so a refresh rejected hours after startup
does not move `auth_state` — the proxy only finds out on the next live view.
Catching that needs a hook on the refresh path itself, not another status
field. The package example still approximates it from the watchdog counter.

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

- **Logging in to Blink from the integration.** The **Blink Authentication**
  panel does not change this: the integration forwards an authenticated
  administrator's form straight to the proxy's `/auth/*` routes and shows the
  redacted state that comes back. It never contacts Blink, never stores the
  credentials or the PIN, and never holds the refresh token. That is what keeps
  credentials in one place, and what makes it safe to run a second Home
  Assistant against the same proxy for testing.
- **Retrying a failed login automatically.** Blink throttles hard — five
  attempts in 55 seconds and everything after fails, with a text each time.
  Every retry path here has a cap and a cooldown for that reason.
