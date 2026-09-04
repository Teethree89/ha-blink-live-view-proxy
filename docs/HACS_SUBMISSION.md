# HACS default submission

Being in HACS's **default** list means someone finds this by typing "blink" into
HACS, instead of pasting a repository URL into the custom-repository dialog.
That is the whole prize. It changes nothing about how the integration works and
nothing about how it updates — it changes who ever finds out it exists.

This file is the checklist for getting there, and the reasoning behind each
item, so the next person does not have to re-derive it from the HACS source.

Everything below was checked against the validators HACS actually runs, in
[`hacs/integration`](https://github.com/hacs/integration/tree/main/custom_components/hacs/validate),
not against the prose docs, because the two have drifted before.

## Where this stands

**As of 0.7.0, all three blockers below have landed**, along with the second
of the three setup fixes (the panel before a config entry). What is left is
mechanical: cut the release after a green run, then open the PR to
`hacs/default`. The reasoning is kept here because the next person to touch
`validate.yaml` or `hacs.json` should know why each line is the way it is.

Green already, and none of it needs touching:

| Check | Why it passes |
|---|---|
| `archived` | Public, not a fork, not archived |
| `description` | Set on the repository |
| `topics` | Eight of them |
| `issues` | Issues are enabled |
| `license` | MIT, on the OSI "popular / strong community" list HACS hardcodes |
| `information` | `README.md` at the root |
| `hacsjson` | Validates against `HACS_MANIFEST_JSON_SCHEMA` |
| `integration_manifest` | All six required keys present |
| `brands` | See below — this one is newly free |
| Releases | Ten of them, `v0.6.2` latest |
| Hassfest | Green |

### Brands stopped being a problem

The [icons PR to `home-assistant/brands`](https://github.com/home-assistant/brands/pull/11094)
was closed within a minute by a bot, which reads like a rejection and is not
one. Since Home Assistant 2026.3, custom integrations carry their own brand
assets and the brands repository no longer accepts them.

HACS followed. Its `brands` validator now looks for
`custom_components/<domain>/brand/icon.png` **first**, and only falls back to
querying the brands repository when that file is absent. That file exists here,
so the check passes on its own. The `ignore: brands` line in CI is left over
from before that landed, and it is now the thing standing in the way — see
blocker 1.

## Blockers

Three, all small, all mechanical.

### 1. CI ignores a check it would now pass

[`.github/workflows/validate.yaml`](../.github/workflows/validate.yaml) passes
`ignore: brands` to the HACS action. The inclusion docs are explicit that the
action must pass "without any errors **or ignores**" — an ignore is treated the
same as a failure, on the reasoning that a repository which needs to silence a
check is not ready.

Delete the two lines. The check passes without them.

### 2. Users never see any of the strings we wrote

The integration ships
[`strings.json`](../custom_components/blink_liveview_proxy/strings.json) and no
`translations/` directory.

`strings.json` is a **build-time** file. Home Assistant Core runs a step that
compiles it into `translations/en.json` before shipping; custom integrations get
no such step, and at runtime Home Assistant reads only
`<integration>/translations/<language>.json`. With that directory missing, every
string in that file is dead weight: the config flow, the options flow and all
three repair issues render raw key paths instead.

This is not a HACS check and nothing in CI catches it. It is still the first
thing anyone installing this will see.

The fix is a copy — the two files can be byte-identical — plus a CI step that
fails when they drift, because a hand-maintained duplicate is exactly the
failure mode already documented for `proxy/` and `addon/proxy/` in
[ROADMAP.md](ROADMAP.md).

### 3. The declared Home Assistant floor is too low

`hacs.json` declares `"homeassistant": "2024.6.0"`. HACS enforces that floor at
install time and refuses to install below it, which makes the number a promise
rather than a hint.

It is wrong.
[`BlinkLiveviewProxyOptionsFlow`](../custom_components/blink_liveview_proxy/config_flow.py)
reads `self.config_entry` without ever setting it. That property was only added
to `OptionsFlow` in **2024.11**; before that, the class had no such attribute
and the modern no-argument constructor pattern did not exist. On 2024.6 through
2024.10 the integration installs cleanly, sets up cleanly, and then raises
`AttributeError` the moment anyone opens Options.

Raise the floor to `2024.11.0`. Backfilling support for four old releases nobody
is asking for is the worse trade.

Note that this floor is also read back by the panel — `prerequisites.py` reports
it as the `home_assistant` row — so the two move together.

## Dependencies

Worth separating, because three different systems have three different rules and
only one of them is enforced.

### What HACS enforces: nothing

HACS has no dependency mechanism. It does not read `requirements`, it does not
resolve dependencies between repositories, and it has no concept of "install
this card first". A HACS repository cannot declare that it needs another HACS
repository, in the default list or out of it.

That is not an oversight to work around; it is the reason
[`prerequisites.py`](../custom_components/blink_liveview_proxy/prerequisites.py)
exists, and the design there is already the correct response to it.

### What Home Assistant enforces

**`requirements`** — absent from our manifest, which is right. The rule for
custom integrations is that it "should only include requirements that are not
required by the Core requirements.txt". The integration imports only `aiohttp`
and `voluptuous`, both of which Core already ships. Listing them would make
Home Assistant pip-check them on every setup for no gain, and risks pinning
against Core.

The proxy's real dependencies — `blinkpy==0.25.9`, `aiohttp`, `certifi` — belong
to the proxy, install with the proxy, and must stay out of the integration
manifest. They run on a different machine in two of the three install paths.

**`dependencies` and `after_dependencies`** — both must reference *built-in*
integrations only. Ours do:

```
dependencies:        frontend, http, panel_custom
after_dependencies:  blink, camera, hassio, lovelace, stream
```

All eight are core domains. `blink` in `after_dependencies` rather than
`dependencies` is the correct choice and worth not "fixing" later: the official
Blink integration is optional here, so a hard dependency would refuse to set up
for anyone who has not installed it.

Hassfest's `_validate_dependencies` — the check that would catch a
non-existent or duplicated domain — only runs when validating the whole core
tree, not with `--integration-path`. So CI is not actually checking this for us,
and a typo here would ship. It is correct today by inspection, not by test.

### The three dependencies nobody can express

These are real, and no manifest key or HACS field can carry them. They are the
reason the Overview tab exists.

1. **The proxy.** The integration is useless without a service that HACS cannot
   install, on a host it may not be able to reach. This is the single largest
   thing a reviewer might raise, and there is no documented rule against it —
   plenty of default integrations talk to something the user must run.
2. **`ffmpeg` and `blinkpy`, on the proxy host.** Both are the proxy's problem
   and both are already covered by the add-on and Docker images. Only the
   systemd path can be missing them, and `install-proxy.sh` handles it on apt
   systems.
3. **`button-card` and `auto-entities`.** Other HACS repositories, needed only
   by the self-populating example dashboard. Both are already marked
   `required=False` in `prerequisites.py`, which is the right posture — a
   default-list integration that hard-requires two other HACS downloads to do
   anything would deserve the complaints it got.

## Setup: three fixes, not a wizard

The instinct is to build a setup wizard. Most of one already exists — the admin
panel is registered in `async_setup`, so it is in the sidebar from the first
restart after installing, before any config entry, with Overview, Cameras,
Authentication and YAML tabs. That is a wizard's content.

The gaps are ordering, not screens.

### 1. Discovery for the add-on path

There is no `async_step_hassio`. The config flow already reads the token the
add-on hands off, but only once the user has thought to go and add the
integration. Discovery inverts that: install the add-on, and Home Assistant
offers "Blink Liveview Proxy discovered" with URL and token filled in.

Highest value of anything on this page. `hassio` is already in
`after_dependencies`, so the wiring is mostly present.

### 2. The panel is a dead end before setup

Every view calls `_runtime_entry()`, which raises `503 "not configured"` when no
config entry exists. So the sidebar item appears on first restart — exactly when
someone most needs the install guidance — and shows an error.

Overview should render the prerequisite rows and the A/B/C install paths without
an entry. The rows that need the proxy can read "cannot be asked yet", which the
three-state design already supports.

### 3. Blink login inside the config flow

After the token validates, if `auth_status` reports unauthenticated, ask for
email and password and then the PIN, in the flow. Today that means finding the
sidebar panel afterwards, and that hop is where the 2FA PIN — which Blink issues
on demand and expires quickly — goes stale.

## Submitting

Once the blockers are done:

1. Land the fixes, confirm the HACS action is green **with no ignores**.
2. Cut a new GitHub release. HACS wants a release created *after* the passing
   run, not a re-tag of an older one.
3. Fork [`hacs/default`](https://github.com/hacs/default), branch from `master`.
4. Add `Teethree89/ha-blink-live-view-proxy` to the `integration` file, in
   alphabetical position.
5. Open the PR from a personal account, not an organization.

`country` in `hacs.json` is for integrations that only work in some regions.
Blink is not one, so leave it out.

## Order of work

Blockers 2 and 3 are user-facing bugs and worth shipping regardless of whether
the submission ever happens. Blocker 1 is thirty seconds and only matters for
the submission.

```
0.7.0   translations/en.json + drift check     (blocker 2)   done
        homeassistant floor -> 2024.11.0       (blocker 3)   done
        drop `ignore: brands`                  (blocker 1)   done
        Overview without a config entry        (setup 2)     done
        -> release, then open the hacs/default PR

next    async_step_hassio                      (setup 1)
        Blink login inside the config flow     (setup 3)
```
