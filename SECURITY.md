# Security Policy

This project is maintained by [@Teethree89](https://github.com/Teethree89),
who is also its security contact. Vulnerability reports go to them privately,
are triaged and fixed by them, and are disclosed through this repository's
security advisories.

## Reporting a vulnerability

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/Teethree89/ha-blink-live-view-proxy/security/advisories/new)
(also on the repository's **Security** tab). Please do not open a public
issue, pull request or discussion about it until a fix is released.

A useful report says:

- which part is affected — the proxy, the Home Assistant integration, or both
  — and which version;
- how it is installed: the add-on, the standalone image, Docker Compose, or
  systemd, and on which kind of Home Assistant install;
- whether the proxy is bound to `127.0.0.1` or to the LAN, and whether
  `BLINK_PROXY_TOKEN` is set;
- the steps to reproduce it and what an attacker gains.

Leave out real Blink passwords, PINs, tokens and the contents of the
`auth_file`. Redact them from any logs you attach.

## What happens next

This is a one-maintainer project, so these are aims, not guarantees:

- an acknowledgement within a week;
- a fix released as a new patch version as soon as one is ready, with the
  reporter kept up to date until then;
- a published GitHub security advisory for every confirmed vulnerability,
  crediting the reporter unless they would rather not be named.

## Supported versions

Only the latest release gets security fixes. They are not backported to older
versions, so the fix for an older install is to upgrade.

## Scope

In scope is the code in this repository:

- the proxy service (`proxy/`), in every form it ships: the Home Assistant
  add-on (`addon/`), the standalone image
  `ghcr.io/teethree89/ha-blink-live-view-proxy`, and the Docker Compose and
  systemd examples;
- the Home Assistant integration (`custom_components/blink_liveview_proxy/`),
  including its panel, live-view dialog and the routes it registers.

That covers, for example, getting past the `BLINK_PROXY_TOKEN` check on the
control routes, the proxy token or a user's Blink credentials reaching a
browser, a log or a URL, and reading files the clip, download or asset routes
were never meant to serve.

Out of scope here, and better reported to whoever can fix it:

- **Blink's and Amazon's cloud services, apps and cameras.** Report those to
  Amazon. Please do not test Blink's servers on this project's behalf.
- **[blinkpy](https://github.com/fronzbot/blinkpy)**, which does the Blink
  login and token refresh, and
  **[mpegts.js](https://github.com/xqq/mpegts.js)**, the bundled player
  library: report to those projects. If this project is exposed through one of
  them, a report here is welcome too.
- **Home Assistant itself**: see
  [home-assistant.io/security](https://www.home-assistant.io/security/).
- **Setups the [Security Notes](README.md#security-notes) warn against**, such
  as binding the proxy to the LAN without a token. Those are not
  vulnerabilities in themselves, but if the docs or defaults make that mistake
  easy, open an ordinary issue.
