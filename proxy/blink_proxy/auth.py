"""Proxy request authorization helpers."""

from __future__ import annotations

import secrets
import urllib.parse

from aiohttp import web

def tokens_match(expected: str, provided: str) -> bool:
    """Compare tokens in constant time, tolerating non-ASCII input.

    secrets.compare_digest() raises TypeError on a non-ASCII str, which would
    turn a malformed Authorization header into a 500 instead of a 401.
    """
    return secrets.compare_digest(
        str(expected).encode("utf-8"), str(provided).encode("utf-8")
    )

def is_authorized(request: web.Request) -> bool:
    """Whether this request carries the proxy token, without rejecting it.

    For fields that are fine to omit but not fine to hand out: a version number
    tells an unauthenticated caller which release to look up exploits for, and
    /status is deliberately reachable without a token.
    """
    token = request.app.get("proxy_token")
    if not token:
        return True

    provided = request.query.get("token", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1]
    return tokens_match(token, provided)

def check_authorized(request: web.Request) -> None:
    token = request.app.get("proxy_token")
    if not token:
        return

    provided = request.query.get("token", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1]
    if not tokens_match(token, provided):
        raise web.HTTPUnauthorized(text="Missing or invalid proxy token\n")

def check_auth_control_authorized(request: web.Request) -> None:
    """Require a configured bearer header for credential-handling routes.

    Unlike media routes, authentication control never accepts a query token:
    URLs are copied, logged, cached, and placed in browser history too easily.
    """
    token = request.app.get("proxy_token")
    if not token:
        raise web.HTTPServiceUnavailable(
            text="Browser authentication requires a configured proxy token\n"
        )

    auth_header = request.headers.get("Authorization", "")
    provided = ""
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1]
    if not tokens_match(token, provided):
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
