# Operations

What breaks, what actually fixes it, and which "fix" makes it worse.

## Reload, restart, re-auth are not interchangeable

They look similar from a dashboard and they are not. Picking the wrong one
costs you 2FA texts and, eventually, a rate-limited account.

| | What it does | Costs a login | Fixes |
|---|---|---|---|
| **Reload** | Restarts the *Home Assistant* Blink config entry | **Yes**, every time | A stuck integration: entry loaded, entities gone stale |
| **Restart** | Restarts the *proxy* service or add-on | Only if the cached token is dead | A wedged proxy: `/health` down, live views hanging |
| **Re-auth** | Full OAuth sign-in, SMS code, new refresh token | Yes, and needs a human | Expired or rejected credentials |

The trap is that **reload cannot fix credentials**, but it is the cheapest
button to press, so it gets pressed repeatedly. Every press re-runs the full
blinkpy login, and under OAuth v2 with SMS 2FA that texts a fresh code each
time.

> On 2026-08-18 a stale token dropped the Blink entry into `setup_retry`.
> A watchdog reloaded it every five minutes. Every reload sent another text,
> until Blink rate-limited the account with HTTP 406 — at which point even a
> correct re-auth failed until the limit aged out.

That is why the reload script in
[`examples/homeassistant-package.yaml`](../examples/homeassistant-package.yaml)
refuses unless all four hold:

1. Something is actually wrong — cameras missing
2. The last reload was over five minutes ago
3. The integration is not already down, so HA is not already retrying on its own backoff
4. Fewer than three reloads have failed in a row

Guard 3 is the one people leave out. When the entry is in `setup_retry` HA is
*already* retrying with backoff; adding reloads on top multiplies the login
attempts rather than replacing them.

## Which one do I need

| Symptom | Cause | Do this | Not this |
|---|---|---|---|
| Tap does nothing, no error anywhere | Dashboard resource not registered | Register `blink-liveview-dialog.js` | Anything else — check this first |
| Some cameras stale, others fine | Integration stuck | Guarded reload | — |
| Every Blink entity unavailable | Credentials expired | Re-auth | Reload, repeatedly |
| `/health` not answering | Proxy wedged | Restart the proxy | Re-auth |
| Watchdog attempts stuck at 3 | Refresh token rejected | Re-auth | More restarts |
| iPhone shows E-001b | Browser has no Media Source Extensions | Native HLS playback | — |
| `ffmpeg exited with 1` | Could be anything; stderr is discarded | Check the proxy log first | Guessing |

## The watchdog

The optional systemd watchdog (see [INSTALL.md](INSTALL.md)) greps the journal
for a stuck token-refresh loop and restarts the service. It deliberately gives
up after three restarts in thirty minutes.

That cap is not timidity. A restart only helps when the refresh loop is *stuck*;
if the refresh token has actually been rejected, each restart is another full
login attempt, and three is already generous. When you see it parked at three
attempts, the session needs a human, not another restart.

`GET /status` reports `watchdog_attempts` and `watchdog_last_restart` so this
is visible from Home Assistant rather than only in the journal.

## Reading `/status`

```bash
curl http://127.0.0.1:8088/status
```

- `ready` and `cameras_discovered` are the liveness signals.
- `cameras_discovered` below `cameras_configured` means the proxy started but
  Blink did not return every camera — usually a session problem.
- `token_seconds_remaining` **going negative is normal**. BlinkPy refreshes
  lazily, inside the request that needs the token, so an idle proxy sits with
  an expired token until the next live view. It is not a fault and not worth
  alerting on.

What is worth alerting on is whether a *refresh* can still succeed, which is a
different question — a rejected refresh token falls through to a full login
flow that needs a human. `binary_sensor.blink_needs_attention` in the package
example approximates this from the watchdog counter and proxy health.

## Re-authenticating

Depends on how you run the proxy.

**Add-on.** Put the code in the `blink_2fa_code` option while the add-on is
waiting. Do not restart it — the code is tied to the login session that asked
for it, and restarting starts a new session with a new `hardware_id`, so the
code you were sent can never match.

**Linux service.** Run the CLI once with the code in the environment:

```bash
BLINK_USERNAME='you@example.com' BLINK_PASSWORD='...' BLINK_2FA_CODE='123456' \
  /opt/blink-liveview-proxy/.venv/bin/python \
  /opt/blink-liveview-proxy/blink_liveview_proxy.py \
  --config /etc/blink-liveview-proxy/config.json list
```

Once it succeeds the refresh token in `auth_file` takes over and restarts are
silent again.

### If it keeps failing immediately

Check that `hardware_id` in the auth file is a real UUID. Blink rejects a
malformed one with a bare **HTTP 406** before the request reaches the login
flow, which reads as "wrong password" and is not. Deleting the auth file and
logging in again mints a fresh one.

### If you get texts you did not ask for

Something is retrying the login endpoint on a loop — a reload automation, a
config entry in `setup_retry`, or a watchdog without a cap. Disable the Blink
config entry to stop the bleeding, then find the loop before re-enabling.
