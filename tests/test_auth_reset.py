"""Regression coverage for the admin-only, restart-safe Blink auth reset.

Run with: python tests/test_auth_reset.py
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_authentication import FakeClient, wait_finished
from proxy.blink_proxy import routes
from proxy.blink_proxy.auth_flow import AuthenticationController

ROOT = pathlib.Path(__file__).resolve().parent.parent


def check(value: bool, reason: str) -> None:
    if not value:
        raise AssertionError(reason)
    print(f"  PASS {reason}")


async def test_reset_active_and_persisted() -> None:
    print("reset cancels PIN, clears cache, survives a restart")
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        cache = base / "blink-auth.json"
        cache.write_text('{"refresh_token":"old-token","hardware_id":"old"}')
        clients = []
        published = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            clients.append(client)
            return client

        controller = AuthenticationController(
            {"auth_file": "blink-auth.json"}, base,
            client_factory=factory, on_client=published.append,
        )
        await controller.start_browser_login("person@example.com", "secret")
        check(controller.status()["state"] == "waiting_for_pin", "challenge started")
        reset = await controller.reset_authentication()
        check(reset["state"] == "idle" and reset["authenticated"] is False,
              "reset clears state and active login")
        check(not cache.exists(), "reset deletes saved Blink refresh token")
        check((base / "blink-auth.json.reset").exists(), "reset writes reboot hold marker")
        check(clients[0].closed, "reset closes the pending Blink OAuth client")
        check(published == [None], "reset detaches the client from the proxy")

        calls_before = len(clients)
        restarted = AuthenticationController(
            {"auth_file": "blink-auth.json"}, base, client_factory=factory,
        )
        idle = await restarted.start_startup_login()
        check(idle["state"] == "idle" and len(clients) == calls_before,
              "restarting with saved add-on credentials does not auto-login")

        await restarted.start_browser_login("person@example.com", "new-secret")
        check(restarted.status()["state"] == "waiting_for_pin",
              "new browser login works after a reset")
        await restarted.submit_pin(restarted.challenge_id, "246810")
        await wait_finished(restarted)
        check(restarted.status()["state"] == "success", "new PIN completed login")
        check(cache.exists(), "new login persists a fresh Blink session")
        check(not (base / "blink-auth.json.reset").exists(),
              "successful manual login permits future startup authentication")
        await restarted.close()
        await controller.close()


async def test_reset_healthy_session() -> None:
    print("reset an already logged-in Blink account")
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        events = []
        controller = AuthenticationController(
            {"auth_file": "auth.json"}, base, client_factory=FakeClient,
            on_client=events.append,
        )
        await controller.start_browser_login("person@example.com", "secret")
        await controller.submit_pin(controller.challenge_id, "246810")
        await wait_finished(controller)
        current = controller.client
        check(current is not None and (base / "auth.json").exists(),
              "working session created an auth cache")
        await controller.reset_authentication()
        check(current.closed and controller.client is None,
              "reset closes live Blink session and removes proxy reference")
        check(events == [current, None], "proxy callback receives cleared client")
        check(not (base / "auth.json").exists(), "working session cache deleted")
        await controller.reset_authentication()
        check(controller.status()["state"] == "idle", "reset is repeatable")
        await controller.close()


async def test_reset_route() -> None:
    print("reset endpoint requires bearer token and typed confirmation")
    with tempfile.TemporaryDirectory() as tmp:
        controller = AuthenticationController(
            {"auth_file": "auth.json"}, pathlib.Path(tmp),
            client_factory=FakeClient,
        )
        app = web.Application()
        app["proxy_token"] = "proxy-secret"
        app["auth_controller"] = controller
        app.router.add_post("/auth/reset", routes.auth_reset_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            valid = {"confirm": "RESET BLINK AUTH"}
            header = {"Authorization": "Bearer proxy-secret"}
            resp = await client.post("/auth/reset", json=valid)
            check(resp.status == 401, "unauthenticated request rejected")
            resp = await client.post("/auth/reset?token=proxy-secret", json=valid)
            check(resp.status == 401, "query-string token rejected")
            resp = await client.post("/auth/reset", json=valid,
                                     headers={"Authorization": "Bearer incorrect"})
            check(resp.status == 401, "wrong bearer rejected")
            resp = await client.post("/auth/reset", json={}, headers=header)
            check(resp.status == 400, "missing confirmation rejected")
            resp = await client.post("/auth/reset",
                                     json={"confirm": "RESET BLINK AUTH", "extra": True},
                                     headers=header)
            check(resp.status == 400, "unexpected fields rejected")
            resp = await client.post("/auth/reset", json=valid, headers=header)
            check(resp.status == 200, "explicitly confirmed authorized reset succeeds")
            state = await resp.json()
            check(state["state"] == "idle" and not state["authenticated"],
                  "reset returns safe redacted idle state")
            check("proxy-secret" not in str(state), "proxy token never reaches response")
        finally:
            await client.close()
            await controller.close()


def test_ha_contract() -> None:
    print("Home Assistant and dashboard expose confirmation-only reset")
    component = ROOT / "custom_components/blink_liveview_proxy"
    views = (component / "views.py").read_text()
    api = (component / "api.py").read_text()
    frontend = (component / "frontend/blink-proxy-auth-panel.js").read_text()
    check('{"login", "pin", "cancel", "reset"}' in views, "reset is on HA action allowlist")
    check('body != {"confirm": "RESET BLINK AUTH"}' in views,
          "HA validates confirmation before forwarding")
    check('payload = await client.async_reset_auth()' in views,
          "HA forwards the reset through its authenticated proxy client")
    check('"POST", "reset", { confirm: confirmation }' in frontend,
          "dashboard sends reset confirmation in a POST body")
    check('id="reset-auth"' in frontend and "window.prompt(" in frontend,
          "dashboard requires typed confirmation")
    check('"/auth/reset"' in api, "proxy reset client uses the protected auth endpoint")


async def main() -> None:
    await test_reset_active_and_persisted()
    await test_reset_healthy_session()
    await test_reset_route()
    test_ha_contract()


if __name__ == "__main__":
    asyncio.run(main())
