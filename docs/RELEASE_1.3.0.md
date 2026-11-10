# 1.3.0 release validation

This records the scope of the November 10, 2026 release work. The reports in
[#88](https://github.com/Teethree89/ha-blink-live-view-proxy/issues/88) distinguish
the released 1.2.1 proxy from PR #89's branch builds.

## Reported findings

| Finding | Resolution |
|---|---|
| Marketplace replaces HACS on current HA | Version-specific installation/update text; older HACS instructions retained |
| Apps replaces the old frontend panel/navigation | Current Settings navigation; retain the supported Python `hassio` helpers and API settings |
| Deprecated build.yaml | Removed; Dockerfile has the pinned base image default |
| Slow aarch64 build reports an early API error | Background installation, Supervisor-job reconciliation, serialized flows, polling before retry |
| Supervisor scans example configuration as an app | Renamed both copies to example-config.json; installer and docs updated |
| Required credential option keys | Complete empty-string fragment and API guidance in the app docs |
| Migration creates duplicate entities | Document a new port, Configure the existing entry before retiring the old app, and recovery from setup_retry |
| Overview shows stale proxy URL | Options override the original entry data, with regression coverage |
| Recovery assumes systemd | Supervisor/container/manual advice follows known installation information |
| PIN error log is unhelpful | Log the safe public authentication explanation, never upstream request details |
| Invalid login/PIN request becomes 502 | Preserve 400/409 with safe, actionable error payloads |
| Reauthentication PIN expectations | Explicit new-PIN warning for reauthentication as well as reset |
| Hand-copied integration is not store-tracked | Document adoption through the same Marketplace/HACS listing and keeping the existing entry |
| Test integration installs default-branch proxy | Explain separate branch proxy testing and rehearsal's 1.3.0 proxy requirement |
| Expiry is reported only as discarded | Record and display the expiry cleanup reason |
| Two cameras fail snapshots | Separate pre-existing camera investigation; no claim that this release fixes it |

## Automated coverage

- All 28 standalone test scripts, including nine installation lifecycle,
  authorization, redaction and cleanup tests.
- 58 HA tests on 2026.2 and 2026.11 locally; CI also covers 2026.9. Supervisor
  is simulated for consent, existing-installation preservation, cancellation,
  install failures/retry and reconciliation of a still-running build.
- Ruff, tracked Python compilation, JavaScript parsing and shell syntax checks.
- A real fresh virtualenv/dependency install and temporary proxy on macOS;
  health, generated-token authentication and complete process/data cleanup.
- Native amd64 and aarch64 image jobs build the actual app and standalone
  Dockerfiles, then run a fresh virtualenv/runtime rehearsal inside each image.
- Standalone image jobs start with an empty volume and network disabled, then
  recreate the container and verify config, generated token and an auth-file
  fixture persist. This fixture establishes file preservation, not Blink login.

## Real-device evidence and limits

@bbolinger tested integration commit `2910211` on fresh amd64 HAOS 18.5 / Core
2026.11.0 / Supervisor 2026.10.5. The integration installed released proxy 1.2.1;
first Blink PIN, discovery, snapshot, live view and saved-session reuse passed.
The branch proxy separately passed rehearsal creation, connection, discard,
cancellation and expiry, leaving the live proxy unchanged.

@fritzzetik tested released 1.2.1 on aarch64 HAOS 18.4 / Core 2026.11.0 /
Supervisor 2026.10.5: manual app installation, first Blink PIN, token/URL
handoff, discovery, live view and session reuse passed. This did not exercise
the new guided installer or temporary rehearsal on aarch64.

No automated test contacts Blink or uses a household session. On the final
release, these remain useful real-device follow-ups:

- Guided installation and existing-app preservation on aarch64 Supervisor.
- Matching final integration/app builds through first Blink PIN and live view.
- Optional independent Blink login during the temporary rehearsal.
- Fresh Docker Blink login and saved-session reuse after container recreation.
- The two-camera snapshot problem, with its own sanitized diagnostics.

## Rollout and recovery

Update the integration and companion proxy to 1.3.0 for Test installation.
Existing camera connections retain the 0.9.0 minimum; older working proxies
do not need a forced authentication reset. Keep existing data volumes, app
data, tokens and config entries. Do not add a duplicate HA integration.

If rollback is needed, restore integration v1.2.1 through the store and restart
HA. Pin standalone Docker to image `v1.2.1` and recreate it with its existing
volume. For Supervisor use its pre-upgrade app backup; for systemd reinstall
with `VERSION=v1.2.1 FORCE=1` through the documented bootstrap. Do not reset
Blink authentication to change software versions. A real PIN challenge still
requires entering the newly issued PIN without restarting its proxy process.
