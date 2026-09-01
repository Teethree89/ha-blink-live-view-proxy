# Development

## Add-on Proxy Sync

The `addon/proxy/` directory is a copy of `proxy/` bundled for the Docker build
context. If you change proxy source files, mirror the changes in both places:

```bash
rsync -av --delete proxy/ addon/proxy/
```

## Validate

From this folder:

```bash
python3 -m py_compile custom_components/blink_liveview_proxy/*.py
python3 -m py_compile proxy/blink_liveview_proxy.py proxy/blink_proxy/*.py
node --check custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js
```

## Local Proxy Run

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r proxy/requirements.txt
cp proxy/config.example.json proxy/config.json
python proxy/blink_liveview_proxy.py --config proxy/config.json list
python proxy/blink_liveview_proxy.py --config proxy/config.json serve
```

## Endpoint Smoke Tests

```bash
curl http://127.0.0.1:8088/health
curl http://127.0.0.1:8088/cameras
curl http://127.0.0.1:8088/clips?source=local&hours=24&limit=5
```

## Home Assistant Static Asset

The player loads:

```text
/api/blink_liveview_proxy/static/mpegts.min.js
```

Dashboards should load:

```text
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
```

That keeps the frontend helper inside the custom integration instead of
requiring a separate `/config/www` copy.

## Tests

No Home Assistant, no Blink account, no network. From the repo root:

```bash
pip install pyyaml -r proxy/requirements.txt
python tests/test_playlist.py        # HLS playlist rewriting
python tests/test_assets.py          # shipped YAML/JSON, manifest, generator
python tests/test_login_decisions.py # stale-PIN handling, PTT eligibility
python tests/test_authentication.py  # browser/CLI/add-on authentication
python tests/test_ffmpeg_log.py      # ffmpeg failure messages
python tests/test_frontend_resource.py  # Lovelace resource registration
```

CI runs all six on every pull request, alongside a compile pass over every
tracked Python file and a syntax check on the shell scripts.

`test_authentication.py` is the guard on the authentication rewrite: route
authorization, one-challenge-at-a-time, same-session PIN delivery, auth-cache
persistence, stale/expired challenges, shutdown, redaction, and the CLI and
add-on paths that must keep working. It fakes BlinkPy entirely — see the
Blink rule below.

Every check in `test_assets.py` exists because something actually broke — a
button-card style written as a list of lists and silently ignored, `hacs.json`
carrying manifest keys that HACS rejects, manifest keys sorted by Home
Assistant core's rule rather than hassfest's. Add to it when you fix a bug
that a file could have caught.

**Nothing in CI may contact Blink.** A login attempt on every run would be
rate-limited within minutes and would text the account owner each time. The
dashboard generator's `--demo` mode exists partly so its output can be tested
without a proxy.

## Cutting a release

The version lives in **three** places and they have to move together. Miss one
and it fails quietly in a different way each time.

| Where | Read by | If you forget |
|---|---|---|
| `custom_components/blink_liveview_proxy/manifest.json` | HACS | every release looks identical in the UI |
| `addon/config.yaml` | Supervisor | add-on users are never offered the update |
| the git tag | HACS, and people pinning | HACS installs the default branch instead |

Steps:

1. Check `main` is green — HACS, Hassfest and Tests.
2. Bump `version` in `manifest.json` and `addon/config.yaml` to the same value.
3. Commit that on its own, so the bump is easy to find later.
4. Tag it: `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`
5. Publish a GitHub release on that tag. HACS only offers tagged releases, so
   an untagged commit reaches nobody, and a draft release reaches nobody either.

While this is pre-1.0, bump the minor for anything user-visible — new
behaviour, a dropped architecture, a changed default — and the patch for fixes
that change nothing about how it is used.
