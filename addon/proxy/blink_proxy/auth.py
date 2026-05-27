"""Proxy request authorization helpers."""

from __future__ import annotations

import secrets
import urllib.parse

from aiohttp import web

def check_authorized(request: web.Request) -> None:
    token = request.app.get("proxy_token")
    if not token:
        return

    provided = request.query.get("token", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1]
    if not secrets.compare_digest(str(token), str(provided)):
        raise web.HTTPUnauthorized(text="Missing or invalid proxy token\n")

def rewrite_playlist_for_token(text: str, token: str | None) -> str:
    if not token:
        return text
    quoted = urllib.parse.quote(token, safe="")
    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#") and "?" not in line:
            lines.append(f"{line}?token={quoted}")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"
