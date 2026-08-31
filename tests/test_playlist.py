"""Tests for HLS playlist rewriting.

No Home Assistant, no Blink account, no network. Run from the repo root:

    python tests/test_playlist.py
"""

import importlib.util
import os
import sys

# Loaded by path rather than by package name: importing the package would pull
# in its __init__, which imports Home Assistant, and this test deliberately has
# no dependencies.
_PATH = os.path.join(
    os.path.dirname(__file__), "..", "custom_components",
    "blink_liveview_proxy", "playlist.py",
)
_spec = importlib.util.spec_from_file_location("blink_playlist", _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
tokenise_playlist = _module.tokenise_playlist

PLAYLIST = "\n".join([
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-TARGETDURATION:1",
    "#EXT-X-MEDIA-SEQUENCE:3",
    "#EXTINF:1.000000,",
    "#EXT-X-PROGRAM-DATE-TIME:2026-08-30T00:00:00.000-0500",
    "segment_00003.ts",
    "#EXTINF:1.000000,",
    "segment_00004.ts",
    "",
])

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def main():
    out = tokenise_playlist(PLAYLIST, "abc123")
    lines = out.splitlines()

    check("every segment gets the token",
          all(l.endswith("?token=abc123") for l in lines if l.endswith(".ts") or "?token=" in l),
          out)
    check("both segments rewritten",
          lines.count("segment_00003.ts?token=abc123") == 1
          and lines.count("segment_00004.ts?token=abc123") == 1, out)
    check("tags are untouched",
          all(l.startswith("#") for l in lines if l.startswith("#EXT")))
    check("no tag gained a token",
          not any("token=" in l for l in lines if l.startswith("#")))
    # A blank line must pass through untouched rather than becoming "?token=".
    blank = tokenise_playlist("#EXTM3U\n\nseg.ts\n", "tok").splitlines()
    check("blank lines survive untouched",
          blank == ["#EXTM3U", "", "seg.ts?token=tok"], blank)

    # A token with URL-unsafe characters must be escaped, or the segment
    # request silently loses part of it.
    out = tokenise_playlist("seg.ts\n", "a/b+c=d e")
    check("token is percent-encoded",
          out.strip() == "seg.ts?token=a%2Fb%2Bc%3Dd%20e", out.strip())

    # Applying it twice must not double-append.
    once = tokenise_playlist("seg.ts\n", "tok")
    twice = tokenise_playlist(once, "tok")
    check("idempotent", once == twice, f"{once!r} vs {twice!r}")

    # A playlist that already carries a query string is left alone.
    out = tokenise_playlist("seg.ts?foo=1\n", "tok")
    check("existing query string preserved", out.strip() == "seg.ts?foo=1", out.strip())

    check("empty token still produces a valid URI",
          tokenise_playlist("seg.ts\n", "").strip() == "seg.ts?token=")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed: {', '.join(FAILURES)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
