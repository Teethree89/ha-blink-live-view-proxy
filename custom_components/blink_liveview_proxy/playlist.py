"""HLS playlist rewriting.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

from urllib.parse import quote


def tokenise_playlist(text: str, token: str) -> str:
    """Append the browser token to every segment URI in an HLS playlist.

    Segment URIs are relative and media players do not carry the playlist's
    query string over to them, so without this every segment request arrives
    unauthenticated and is rejected.

    Comments, tags and blank lines are left alone, and a URI that already has a
    query string is not touched, so this is safe to apply more than once.
    """
    quoted = quote(token or "", safe="")
    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#") and "?" not in line:
            line = f"{line}?token={quoted}"
        lines.append(line)
    return "\n".join(lines) + "\n"
