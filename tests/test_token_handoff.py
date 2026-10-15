"""Regression tests for issue #74: add-on token rotation and HA reauth.

Runs with Python's standard library and no Home Assistant installation:
    python -m unittest tests/test_token_handoff.py
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "blink_liveview_proxy"


def load_helper():
    """Extract the token recovery function without importing Home Assistant."""
    source = COMPONENT.joinpath("config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        stmt for stmt in tree.body
        if isinstance(stmt, ast.AsyncFunctionDef)
        and stmt.name == "_async_verified_handoff_token"
    )
    namespace = {"HomeAssistant": object}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "config_flow.py", "exec"),
        namespace,
    )
    return namespace


class TokenHandoffTest(unittest.TestCase):
    URL = "http://addon-blink-liveview-proxy:8088"

    def verify(self, *, handoff_url=None, handoff_token="new-token",
               old_token="old-token", base_url=None, auth_error=None):
        calls = []
        namespace = load_helper()

        class ProxyAuthError(Exception):
            pass

        class ProxyConnectionError(Exception):
            pass

        async def url_reader(_hass):
            calls.append("url")
            return self.URL if handoff_url is None else handoff_url

        async def token_reader(_hass):
            calls.append("token")
            return handoff_token

        def normalize(value):
            value = str(value or "").strip().rstrip("/")
            if not value:
                raise ProxyConnectionError("missing URL")
            return value

        class Client:
            def __init__(self, _session, url, token):
                calls.append(("client", url, token))

            async def async_get_auth_status(self):
                calls.append("verify")
                if auth_error == "unauthorized":
                    raise ProxyAuthError()
                if auth_error == "unreachable":
                    raise ProxyConnectionError()
                return {"state": "idle"}

        namespace.update({
            "_async_handoff_url": url_reader,
            "_async_handoff_token": token_reader,
            "normalize_base_url": normalize,
            "ProxyAuthError": ProxyAuthError,
            "ProxyConnectionError": ProxyConnectionError,
            "BlinkLiveviewProxyClient": Client,
            "async_get_clientsession": lambda hass: object(),
        })
        result = asyncio.run(namespace["_async_verified_handoff_token"](
            object(), self.URL if base_url is None else base_url, old_token
        ))
        return result, calls

    def test_rotated_addon_token_is_verified(self):
        token, calls = self.verify()
        self.assertEqual(token, "new-token")
        self.assertIn(("client", self.URL, "new-token"), calls)
        self.assertIn("verify", calls)

    def test_matching_url_can_be_normalized(self):
        token, _ = self.verify(base_url=self.URL + "/")
        self.assertEqual(token, "new-token")

    def test_unrelated_proxy_never_reads_or_adopts_handoff(self):
        token, calls = self.verify(base_url="http://unrelated:8088")
        self.assertEqual(token, "")
        self.assertEqual(calls, ["url"])

    def test_missing_handoff_url_is_not_trusted(self):
        token, calls = self.verify(handoff_url="")
        self.assertEqual(token, "")
        self.assertEqual(calls, ["url"])

    def test_unchanged_token_does_not_probe(self):
        token, calls = self.verify(old_token="new-token")
        self.assertEqual(token, "")
        self.assertNotIn("verify", calls)

    def test_missing_token_does_not_probe(self):
        token, calls = self.verify(handoff_token="")
        self.assertEqual(token, "")
        self.assertNotIn("verify", calls)

    def test_bad_token_is_not_adopted(self):
        token, calls = self.verify(auth_error="unauthorized")
        self.assertEqual(token, "")
        self.assertIn("verify", calls)

    def test_proxy_unavailable_is_not_adopted(self):
        token, calls = self.verify(auth_error="unreachable")
        self.assertEqual(token, "")
        self.assertIn("verify", calls)

    def test_invalid_url_does_not_probe(self):
        token, calls = self.verify(base_url="")
        self.assertEqual(token, "")
        self.assertEqual(calls, [])

    def test_setup_and_configure_use_validated_token(self):
        config = COMPONENT.joinpath("config_flow.py").read_text()
        setup = COMPONENT.joinpath("__init__.py").read_text()
        self.assertIn("shared = await _async_verified_handoff_token(", config)
        self.assertIn("defaults = {**current, CONF_TOKEN: shared}", config)
        self.assertIn("shared_token = await _async_verified_handoff_token(", setup)
        self.assertIn("merged[CONF_TOKEN] = shared_token", setup)
        # Where the token is written (data or options) is decided in one place,
        # which tests/ha/test_token_reauth.py exercises inside real Home Assistant.
        self.assertIn("**token_update(entry, shared_token)", setup)
        self.assertNotIn("LOGGER.info(shared_token)", setup)


if __name__ == "__main__":
    unittest.main()
