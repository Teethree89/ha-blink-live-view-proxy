# Operations

What breaks, what actually fixes it, and which "fix" makes it worse.

## Reload, restart, re-auth are not interchangeable

They look similar from a dashboard and they are not. Picking the wrong one
costs you 2FA texts and, eventually, a rate-limited account.

| | What it does | Costs a login | Fixes |
|---|---|---|---|
| **Reload** | Restarts the *Home Assistant* Blink config entry | **Yes**, every time | A stuck integration: entry loaded, entities gone stale |
| **Restart** | Restarts the *proxy* service or add-on | Only if the cached token is dead | A wedged proxy: `/health` down, live views hanging |
| **Re-auth** | Full OAuth sign-in, SMS code, new refresh token, in one live session | Yes, and needs a human answering the PIN while it waits | Expired or rejected credentials |

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
| Every Blink entity unavailable | Credentials expired | Re-auth in the **Blink Authentication** panel | Reload, repeatedly |
| `/health` not answering | Proxy wedged | Restart the proxy | Re-auth |
| Watchdog attempts stuck at 3 | Refresh token rejected | Re-auth in the **Blink Authentication** panel | More restarts |
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
- `auth_state` is the login state machine: `idle`, `authenticating`,
  `waiting_for_pin`, `success`, `expired`, or `failure`. `waiting_for_pin` means
  a human owes the proxy a PIN right now; restarting instead loses it.
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

Three ways in, one rule behind all of them: **a single running process holds the
OAuth challenge, and the PIN Blink issues belongs only to that challenge.** Do
not restart between starting a login and entering the PIN, and do not supply a
PIN before a login has started — Blink has not issued one yet.

### Browser (recommended)

The custom integration adds an admin-only **Blink Authentication** panel to the
Home Assistant sidebar. It needs the proxy API token set on both sides, which
both install paths handle: `scripts/install-proxy.sh` writes one to the
service's environment file, and the add-on generates one and shares it with the
integration. On a hand-built install, set `BLINK_PROXY_TOKEN` for the service
and enter the same value in the integration.

1. Open the panel as a Home Assistant administrator.
2. Select **Reauthenticate** if a working session already exists, otherwise the
   login form is shown straight away.
3. Enter the Blink email and password and select **Start login**.
4. Wait for **waiting for PIN**, then read the PIN Blink just sent.
5. Enter that PIN on the same page, before the countdown shown on it runs out.
6. Wait for **success**.

What the proxy guarantees while that runs:

| Situation | Behavior |
|---|---|
| A second login attempt | Rejected with 409; one challenge exists at a time |
| A PIN naming an old challenge | Rejected as stale; start a new login |
| Nobody answers | The challenge expires (fifteen minutes, and Blink's PIN expires well before that) and the state shows **expired** |
| The new login fails | The previously working client keeps serving live views |
| Cancel | Explicit, and only cancels the matching challenge |
| Proxy or add-on restart | The in-memory challenge is gone; start a new login and use a new PIN |

The auth cache is replaced only after the candidate login has fully succeeded,
so a failed reauthentication cannot leave you with a broken cached session. A
*successful* one does swap the Blink session, so a live view open at that exact
moment ends; reopen it afterwards.

The serving process no longer exits when a login needs a human. It starts the
HTTP API first, then logs in behind it, so `/health`, `/status`, and `/auth/*`
answer throughout while the camera, live-view, and clip routes reply `503` until
a session exists. That is what makes browser login possible at all, and it also
removes the old exit-and-restart cycle that could text you a code per restart.
One consequence: `serve` never prompts on a terminal any more. Use the panel, or
the `list` command below, to answer a PIN.

#### If the panel will not start a login

The panel diagnoses this from what the proxy answered, and shows the fix. It
cannot apply it: the proxy is a separate service, on a host Home Assistant has
no shell on. **Check proxy** re-runs the diagnosis after you have fixed it.

| What it says | What happened | Fix |
|---|---|---|
| The proxy has no `/auth` routes | The proxy predates browser authentication | Upgrade the proxy — `sudo scripts/install-proxy.sh`, or the add-on store |
| Running without an API token | A token is not configured, so the routes are refused | Provision one and restart, then enter it in the integration |
| The proxy rejected this token | Home Assistant's token does not match the proxy's | Accept the reauthentication prompt, or fix it in the integration's options |
| Could not reach the proxy | Service down, or the wrong URL | `systemctl status blink-liveview-proxy.service` |

The first one is the common surprise after updating the integration through
HACS: the integration and the proxy version independently, so a 0.3.0
integration will happily talk to an older proxy right up until it needs a route
that proxy has never had.

#### Upgrading the proxy

`scripts/install-proxy.sh`, from a checkout on the proxy host — see
[INSTALL.md](INSTALL.md#2b-1-upgrading-later). It keeps the token, config and
Blink session, updates the virtualenv, and restarts the service.

The virtualenv part matters: the blinkpy pin moves between releases, and a
proxy running new code against an old blinkpy looks completely healthy until
someone tries to authenticate.

### Add-on, without the browser

1. Set `blink_username` and `blink_password`, leave `blink_2fa_code` empty, and
   start the add-on once.
2. When the log says Blink sent a code, paste it into `blink_2fa_code` and
   **save while the add-on keeps running**. Saving is enough — the proxy reads
   the live Supervisor option.
3. Do not restart. Restarting starts a new session with a new `hardware_id`, so
   the code you were sent can never match.
4. Clear `blink_2fa_code` after the success line so it cannot be resubmitted.

### Linux service, without the browser

Run the CLI interactively and answer the prompt it shows *after* Blink issues
the code:

```bash
read -r -p 'Blink email: ' BLINK_USERNAME
read -r -s -p 'Blink password: ' BLINK_PASSWORD; printf '\n'
export BLINK_USERNAME BLINK_PASSWORD
/opt/blink-liveview-proxy/.venv/bin/python \
  /opt/blink-liveview-proxy/blink_liveview_proxy.py \
  --config /etc/blink-liveview-proxy/config.json list
unset BLINK_USERNAME BLINK_PASSWORD
```

Enter the freshly issued code at the `Blink 2FA code:` prompt in that same
session. Do not put a code in `BLINK_2FA_CODE` or `--pin` beforehand: a value
present before sign-in is from an earlier attempt, so the proxy ignores it and
logs a warning naming the source it ignored.

Once it succeeds the refresh token in `auth_file` takes over. Restart
`blink-liveview-proxy.service` afterwards so the long-running process loads the
new cache — that restart happens *after* the PIN, which is safe.

### If it keeps failing immediately

Blink fronts `/oauth/v2/authorize` with Cloudflare, which answers a non-UUID
`hardware_id` with a bare **HTTP 406** before the request ever reaches the
application. A stored value like `Home Assistant` therefore fails every login
with nothing in the response to say why, and it reads as a wrong password.
Verified 2026-08-19: that value 406s while fresh UUIDs get 302.

The proxy now checks this at startup and discards a malformed `hardware_id` so
blinkpy mints a fresh one, logging a warning when it does. That costs one extra
2FA prompt, against a login that could not have succeeded at all.

### Add-on permissions

The add-on asks for two grants beyond its own `/data` directory:

| Grant | Needed? | Why |
|---|---|---|
| `hassio_api: true` | **Yes** | Reads `blink_2fa_code` from `/addons/self/info` while waiting for a PIN. `/data/options.json` is only written at start, so a value typed while waiting never reaches it. Default role, one endpoint, one field. |
| `share:rw` | No — redundancy | A second way to hand over the PIN, via `/share/blink_2fa_pin.txt`. Reachable from the Samba and file editor add-ons. |

`/share` is shared with every other add-on that maps it, so a PIN written there
is briefly readable by them; the proxy deletes the file as soon as it reads it.
To drop the redundancy, remove `share:rw` from `addon/config.yaml` and the
`/share` entry from `PIN_FILES` in `blink_proxy/blink.py`. The Supervisor option
and `/data/blink_2fa_pin.txt` both keep working without it.

### If you get texts you did not ask for

Something is retrying the login endpoint on a loop — a reload automation, a
config entry in `setup_retry`, or a watchdog without a cap. Disable the Blink
config entry to stop the bleeding, then find the loop before re-enabling.

The proxy itself is not that loop: an unanswered or failed login leaves it
running in the `expired`/`failure` state, visible as `auth_state` in `/status`,
and it does not retry on its own. Every login is started deliberately — at
service start, from the panel, or from the CLI.
