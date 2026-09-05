#!/usr/bin/env bash
# One command that installs the proxy, and the same command that upgrades it.
#
#   curl -fsSL https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/scripts/bootstrap.sh | sudo bash
#
# It keeps a checkout on the proxy host, moves it to the newest tag, and runs
# scripts/install-proxy.sh from there. That script is what actually installs:
# code, virtualenv, config, proxy API token, service, watchdog.
#
# Defaults to the newest *tag*, never to main. A one-liner piped into a shell
# should not run whatever was pushed a minute ago, and the timer that reuses
# this script would otherwise track every commit.
#
#   VERSION=0.7.0-rc.1   pin a specific stable or prerelease tag
#   SRC_DIR=...      where the checkout lives (default /opt/src/...)
#   FORCE=1          reinstall even when the tag is already installed
#
# Environment understood by install-proxy.sh (BIND_HOST, PROXY_PORT,
# BLINK_PROXY_TOKEN, INSTALL_WATCHDOG, INSTALL_AUTOUPDATE) is passed through.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Teethree89/ha-blink-live-view-proxy}"
SRC_DIR="${SRC_DIR:-/opt/src/ha-blink-live-view-proxy}"
OPT_DIR="${OPT_DIR:-/opt/blink-liveview-proxy}"
VERSION="${VERSION:-}"

# Sort stable and prerelease tags by semantic version. Both the historical
# vX.Y.Z spelling and HACS-friendly X.Y.Z-rc.N spelling are accepted. Plain
# sort -V is not enough: it considers 0.7.0-rc.1 newer than final 0.7.0.
sort_release_tags() {
  python3 -c '
import re
import sys

def key(tag):
    value = tag.removeprefix("v").split("+", 1)[0]
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", value)
    if not match:
        return None
    release = tuple(int(part) for part in match.group(1, 2, 3))
    prerelease = match.group(4)
    if prerelease is None:
        return (*release, 1, ())
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.findall(r"[A-Za-z]+|\d+", prerelease)
    )
    return (*release, 0, identifiers)

tags = ((key(line.strip()), line.strip()) for line in sys.stdin)
for _, tag in sorted(item for item in tags if item[0] is not None):
    print(tag)
'
}

newest_tag() {
  git -C "$SRC_DIR" tag --list | sort_release_tags | tail -n 1
}

# What is installed right now, as the proxy itself reports it. Absent means an
# install too old to say so, which is always a reason to continue.
installed_version() {
  sed -n 's/^PROXY_VERSION = "\(.*\)"$/\1/p' \
    "$OPT_DIR/blink_proxy/constants.py" 2>/dev/null | head -n 1
}

# The timer reruns this script daily, so the common case is "nothing to do".
should_install() {
  local target="$1" installed="$2"
  [ -n "${FORCE:-}" ] && return 0
  [ -z "$installed" ] && return 0
  [ "${target#v}" = "${installed#v}" ] && return 1
  # An explicit pin may intentionally move either direction. Automatic runs
  # only move forward, so removing a prerelease tag cannot downgrade a host.
  [ -n "$VERSION" ] && return 0
  [ "$(printf '%s\n%s\n' "$installed" "$target" | sort_release_tags | tail -n 1)" = "$target" ]
}

main() {
if [ "$(id -u)" != "0" ]; then
  echo "This needs root: it writes to $OPT_DIR, /etc and /etc/systemd/system." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing git"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y git
  else
    echo "git is required and could not be installed automatically." >&2
    exit 1
  fi
fi

if [ -d "$SRC_DIR/.git" ]; then
  git -C "$SRC_DIR" fetch --tags --prune --quiet
else
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone --quiet "$REPO_URL" "$SRC_DIR"
fi

TARGET="${VERSION:-$(newest_tag)}"
if [ -z "$TARGET" ]; then
  echo "No tags found in $REPO_URL - refusing to guess at main." >&2
  exit 1
fi

INSTALLED="$(installed_version)"
if ! should_install "$TARGET" "$INSTALLED"; then
  echo "Already on $TARGET - nothing to do."
  exit 0
fi

echo "Installing $TARGET (currently ${INSTALLED:-unknown})"
git -C "$SRC_DIR" checkout --quiet "$TARGET"
exec "$SRC_DIR/scripts/install-proxy.sh"
}

# Sourcing this file defines the functions above and runs nothing, which is how
# the tests exercise the tag and version logic without root or a network.
#
# The default matters: piped into bash from curl, the script is read from stdin
# and BASH_SOURCE is unset, which under `set -u` aborted before main ever ran -
# in exactly the invocation this script exists for.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  main "$@"
fi
