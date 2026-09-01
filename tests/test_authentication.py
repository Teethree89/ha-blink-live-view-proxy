"""Focused browser, CLI, and add-on authentication regression tests."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import pathlib
import re
import sys
import tempfile
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proxy.blink_proxy import blink as blink_module
from proxy.blink_proxy import routes
from proxy.blink_proxy.auth_flow import (
    AuthConflictError,
    AuthenticationController,
    AuthFlowError,
    StaleChallengeError,
)

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


class FakeClient:
    """Controllable client that records which challenge receives the PIN."""

    def __init__(
        self,
        config: dict[str, Any],
        base: pathlib.Path,
        _pin: str | None,
        *,
        username: str | None,
        password: str | None,
        pin_provider,
        state_callback,
        fresh_login: bool,
        persist_intermediate: bool,
    ) -> None:
        self.config = config
        self.base = base
        self.username = username
        self.password = password
        self.pin_provider = pin_provider
        self.state_callback = state_callback
        self.fresh_login = fresh_login
        self.persist_intermediate = persist_intermediate
        self.ready = False
        self.closed = False
        self.received_pin: str | None = None
        self.behavior = config.get("test_behavior", "success")

    async def start(self) -> None:
        if self.behavior == "credential_failure":
            raise RuntimeError(f"upstream echoed {self.username}:{self.password}")
        self.state_callback("waiting_for_pin")
        self.received_pin = await self.pin_provider()
        if self.behavior == "invalid_pin" or self.received_pin != "246810":
            raise AuthFlowError("invalid_pin")
        self.ready = True
        auth_file = self.base / self.config["auth_file"]
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(json.dumps({"refresh_token": "saved-refresh"}))
        self.username = None
        self.password = None

    async def close(self) -> None:
        self.closed = True
        self.username = None
        self.password = None
        self.ready = False


async def wait_finished(controller: AuthenticationController) -> None:
    for _ in range(100):
        if controller.status()["can_start"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("authentication task did not finish")


async def test_controller() -> None:
    print("\nauthentication controller")
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        config = {"auth_file": "auth.json"}
        made: list[FakeClient] = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            made.append(client)
            return client

        activated: list[FakeClient] = []
        controller = AuthenticationController(
            config,
            base,
            client_factory=factory,
            on_client=lambda client: activated.append(client),
            timeout_seconds=0.12,
        )

        status = await controller.start_browser_login("person@example.com", "top-secret")
        challenge = status["challenge_id"]
        check(status["state"] == "waiting_for_pin", "login reaches waiting-for-PIN")
        check(bool(challenge), "live challenge has an opaque correlation id")

        try:
            await controller.start_browser_login("other@example.com", "other-secret")
            concurrent = False
        except AuthConflictError:
            concurrent = True
        check(concurrent, "concurrent login is rejected")

        try:
            await controller.submit_pin("stale-challenge", "246810")
            stale = False
        except StaleChallengeError:
            stale = True
        check(stale, "stale challenge cannot receive a PIN")

        await controller.submit_pin(str(challenge), "246810")
        await wait_finished(controller)
        check(made[0].received_pin == "246810", "PIN reaches the same client session")
        check(controller.status()["state"] == "success", "successful state is published")
        check(controller.client is made[0] and activated == [made[0]], "client swaps only on success")
        check(json.loads((base / "auth.json").read_text()) == {"refresh_token": "saved-refresh"}, "successful auth persists the cache")
        public = json.dumps(controller.status())
        check("person@example.com" not in public and "top-secret" not in public, "status contains no credentials")

        previous = controller.client
        config["test_behavior"] = "invalid_pin"
        failed = await controller.start_browser_login("person@example.com", "new-secret")
        await controller.submit_pin(str(failed["challenge_id"]), "111111")
        await wait_finished(controller)
        check(controller.status()["state"] == "failure", "invalid PIN reaches failure")
        check("PIN" in controller.status()["message"], "invalid PIN has a safe actionable message")
        check(controller.client is previous, "failed reauth preserves the active client")

        config["test_behavior"] = "success"
        expired = await controller.start_browser_login("person@example.com", "expiry-secret")
        await wait_finished(controller)
        check(controller.status()["state"] == "expired", "unanswered challenge expires")
        try:
            await controller.submit_pin(str(expired["challenge_id"]), "246810")
            stale_after_expiry = False
        except StaleChallengeError:
            stale_after_expiry = True
        check(stale_after_expiry, "expired challenge rejects a late PIN")

        cancel_status = await controller.start_browser_login("person@example.com", "cancel-secret")
        await controller.cancel(str(cancel_status["challenge_id"]))
        check(controller.status()["state"] == "idle", "cancellation returns to idle")

        config["test_behavior"] = "credential_failure"
        captured: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = Capture()
        logging.getLogger("blink-liveview-proxy").addHandler(handler)
        try:
            await controller.start_browser_login("secret-user@example.com", "credential-secret")
            await wait_finished(controller)
        finally:
            logging.getLogger("blink-liveview-proxy").removeHandler(handler)
        joined = " ".join(captured) + json.dumps(controller.status())
        check("secret-user" not in joined and "credential-secret" not in joined, "errors and logs redact credential failures")
        await controller.close()


async def test_routes() -> None:
    print("\nauthentication route authorization")
    with tempfile.TemporaryDirectory() as tmp:
        controller = AuthenticationController(
            {"auth_file": "auth.json"}, pathlib.Path(tmp),
            client_factory=FakeClient, timeout_seconds=0.12,
        )
        app = web.Application(client_max_size=4096)
        app["proxy_token"] = "proxy-secret"
        app["auth_controller"] = controller
        app.router.add_get("/auth/status", routes.auth_status_handler)
        app.router.add_post("/auth/login", routes.auth_login_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/auth/status")
            check(response.status == 401, "missing bearer token is rejected")
            response = await client.get("/auth/status?token=proxy-secret")
            check(response.status == 401, "query token is rejected on auth routes")
            response = await client.get("/auth/status", headers={"Authorization": "Bearer wrong"})
            check(response.status == 401, "wrong bearer token is rejected")
            response = await client.get("/auth/status", headers={"Authorization": "Bearer pröxy-secret"})
            check(response.status == 401, "a non-ASCII token is rejected, not a server error")
            response = await client.get("/auth/status", headers={"Authorization": "Bearer proxy-secret"})
            check(response.status == 200, "correct bearer header is accepted")
            check(response.headers.get("Cache-Control") == "no-store", "auth response is not cacheable")

            response = await client.post(
                "/auth/login",
                headers={"Authorization": "Bearer proxy-secret"},
                json={"username": "route-user@example.com", "password": "route-password"},
            )
            payload_text = await response.text()
            check(response.status == 202, "authorized login starts")
            check("route-user" not in payload_text and "route-password" not in payload_text, "login response contains no credentials")
            check("route-user" not in str(response.url) and "route-password" not in str(response.url), "credentials are absent from the URL")
            await controller.cancel(json.loads(payload_text)["challenge_id"])
        finally:
            await client.close()
            await controller.close()

        disabled = web.Application()
        disabled["proxy_token"] = ""
        disabled["auth_controller"] = controller
        disabled.router.add_get("/auth/status", routes.auth_status_handler)
        client = TestClient(TestServer(disabled))
        await client.start_server()
        try:
            check((await client.get("/auth/status")).status == 503, "auth control is disabled without a proxy token")
        finally:
            await client.close()


async def test_blink_client_persistence() -> None:
    print("\nBlinkClient same-session persistence")
    original = (blink_module.Auth, blink_module.Blink, blink_module.create_client_session)
    seen: dict[str, Any] = {}

    class FakeSession:
        async def close(self):
            pass

    class FakeAuth:
        def __init__(self, login_data, no_prompt, session, callback):
            self.login_attributes = dict(login_data)
            self.callback = callback
            seen["auth"] = self

    class FakeBlink:
        def __init__(self, refresh_rate, session):
            self.auth = None
            self.cameras = {}
            seen["blink"] = self

        async def start(self):
            raise blink_module.BlinkTwoFARequiredError

        async def send_2fa_code(self, pin):
            seen["pin"] = pin
            seen["same_auth"] = self.auth is seen["auth"]
            self.auth.login_attributes["refresh_token"] = "fresh-refresh"
            self.auth.callback()
            return True

    blink_module.Auth, blink_module.Blink = FakeAuth, FakeBlink
    blink_module.create_client_session = lambda: FakeSession()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            auth_file = base / "auth.json"
            auth_file.write_text(json.dumps({"hardware_id": "08a54f2d-61b3-4ff1-9e74-fbcd89ead8f5", "refresh_token": "old"}))
            states: list[str] = []
            client = blink_module.BlinkClient(
                {"auth_file": "auth.json", "username_env": "TEST_BLINK_USER", "password_env": "TEST_BLINK_PASSWORD", "twofa_env": "TEST_BLINK_PIN", "cameras": {}},
                base, None, username="blink-user@example.com", password="blink-password",
                pin_provider=lambda: asyncio.sleep(0, result="246810"),
                state_callback=states.append, fresh_login=True, persist_intermediate=False,
            )
            await client.start()
            saved = json.loads(auth_file.read_text())
            check(seen.get("same_auth") is True, "PIN is submitted on the challenged BlinkPy Auth object")
            check(seen.get("pin") == "246810", "fresh PIN is submitted")
            check(states == ["waiting_for_pin"], "waiting state is reported")
            check(saved.get("refresh_token") == "fresh-refresh", "BlinkClient commits the refreshed auth cache")
            check("password" not in saved, "auth cache excludes the password")
            check("password" not in seen["auth"].login_attributes, "browser password is scrubbed from BlinkPy state")

            # BlinkPy calls the same callback on every later token refresh.
            seen["auth"].login_attributes["refresh_token"] = "rotated-refresh"
            seen["auth"].callback()
            check(
                json.loads(auth_file.read_text()).get("refresh_token") == "rotated-refresh",
                "later token refreshes still reach the auth cache",
            )
            await client.close()
    finally:
        blink_module.Auth, blink_module.Blink, blink_module.create_client_session = original


def test_compatibility_assets() -> None:
    print("\nCLI and add-on compatibility")
    run_sh = (ROOT / "addon/run.sh").read_text()
    blink_source = (ROOT / "proxy/blink_proxy/blink.py").read_text()
    check(run_sh.count(" serve ") == 1, "add-on still starts one serving process")
    check("export BLINK_USERNAME BLINK_PASSWORD BLINK_PROXY_TOKEN" in run_sh, "add-on still supplies username/password to startup login")
    check("export BLINK_2FA_CODE" not in run_sh, "add-on never pre-supplies a stale PIN")
    check('input("Blink 2FA code: "' in blink_source, "interactive CLI still prompts in the same process")
    check("code = await _wait_for_pin()" in blink_source, "non-interactive add-on fallback still polls for a PIN")


class ReadyClient:
    """Minimal ready client for the unchanged media/inventory routes."""

    ready = True

    def list_cameras(self):
        return [{"slug": "front_door", "name": "Front Door"}]

    def status(self):
        return {
            "ready": True,
            "cameras_discovered": 1,
            "cameras_configured": 1,
            "token_expiration": None,
        }

    async def close(self) -> None:
        return None


async def test_existing_routes_unchanged() -> None:
    print("\nexisting proxy route compatibility")
    with tempfile.TemporaryDirectory() as tmp:
        controller = AuthenticationController(
            {"auth_file": "auth.json"}, pathlib.Path(tmp),
            client_factory=FakeClient, timeout_seconds=0.12,
        )
        app = web.Application()
        app["proxy_token"] = "proxy-secret"
        app["auth_controller"] = controller
        app["client"] = None
        app["config"] = {"cameras": {"front_door": {}}}
        app.router.add_get("/cameras", routes.cameras_handler)
        app.router.add_get("/status", routes.status_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            check((await client.get("/cameras")).status == 401, "media routes still require the proxy token")
            response = await client.get("/cameras?token=proxy-secret")
            check(response.status == 503, "media routes still accept the query token, and wait for login")

            status = await (await client.get("/status")).json()
            check(status["auth_state"] == "idle", "unauthenticated /status publishes only the auth state")
            check("proxy-secret" not in json.dumps(status), "/status leaks no proxy token")

            app["client"] = ReadyClient()
            response = await client.get("/cameras?token=proxy-secret")
            payload = await response.json()
            check(response.status == 200 and payload["cameras"][0]["slug"] == "front_door", "camera inventory is unchanged once authenticated")
            status = await (await client.get("/status")).json()
            check(status["ready"] is True and status["cameras_discovered"] == 1, "/status keeps its existing liveness fields")
        finally:
            await client.close()
            await controller.close()


async def test_restart_safety() -> None:
    print("\nrestart and shutdown safety")
    with tempfile.TemporaryDirectory() as tmp:
        made: list[FakeClient] = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            made.append(client)
            return client

        controller = AuthenticationController(
            {"auth_file": "auth.json"}, pathlib.Path(tmp),
            client_factory=factory, timeout_seconds=5,
        )
        status = await controller.start_browser_login("person@example.com", "shutdown-secret")
        check(status["state"] == "waiting_for_pin", "a challenge is live before shutdown")
        check(
            made[0].fresh_login and not made[0].persist_intermediate,
            "a browser attempt never writes over the cache in use",
        )

        await controller.close()
        check(made[0].closed, "shutdown closes the pending Blink session")
        check(controller.client is None, "shutdown leaves no half-authenticated client")
        check(not pathlib.Path(tmp, "auth.json").exists(), "an interrupted attempt never writes the auth cache")

        try:
            await controller.start_browser_login("person@example.com", "after-close")
            refused = False
        except RuntimeError:
            refused = True
        check(refused, "a closed controller refuses new logins")


def _class_by_name(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name), None
    )


def _decorator_names(node: ast.AST) -> set[str]:
    names = set()
    for decorator in getattr(node, "decorator_list", []):
        if isinstance(decorator, ast.Name):
            names.add(decorator.id)
        elif isinstance(decorator, ast.Attribute):
            names.add(decorator.attr)
    return names


def test_home_assistant_route_authorization() -> None:
    print("\nHome Assistant route authorization")
    views = ast.parse((ROOT / "custom_components/blink_liveview_proxy/views.py").read_text())
    for name in (
        "BlinkLiveviewProxyAuthStatusView",
        "BlinkLiveviewProxyAuthActionView",
    ):
        node = _class_by_name(views, name)
        if node is None:
            check(False, f"{name} exists")
            continue
        requires_auth = any(
            isinstance(stmt, ast.Assign)
            and any(getattr(target, "id", "") == "requires_auth" for target in stmt.targets)
            and getattr(stmt.value, "value", None) is True
            for stmt in node.body
        )
        handlers = [
            stmt
            for stmt in node.body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            and stmt.name in {"get", "post", "put", "delete"}
        ]
        check(requires_auth, f"{name} requires an authenticated Home Assistant user")
        check(
            bool(handlers) and all("require_admin" in _decorator_names(h) for h in handlers),
            f"{name} restricts every handler to administrators",
        )

    init = ast.parse((ROOT / "custom_components/blink_liveview_proxy/__init__.py").read_text())
    panel_calls = [
        node
        for node in ast.walk(init)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "async_register_panel"
    ]
    admin_only = [
        call
        for call in panel_calls
        if any(
            kw.arg == "require_admin" and getattr(kw.value, "value", None) is True
            for kw in call.keywords
        )
    ]
    check(len(panel_calls) == 1 and len(admin_only) == 1, "the auth panel is registered as admin-only")


def test_failure_classification() -> None:
    print("\nproxy failure diagnosis")
    sys.path.insert(0, str(ROOT / "custom_components/blink_liveview_proxy"))
    import failures

    cases = {
        None: ("proxy_unreachable", "reach"),
        404: ("proxy_outdated", "predates"),
        503: ("proxy_token_missing", "without an API token"),
        401: ("proxy_token_mismatch", "rejected"),
        403: ("proxy_token_mismatch", "rejected"),
        500: ("proxy_error", "proxy log"),
    }
    for status, (reason, phrase) in cases.items():
        payload = failures.failure_payload(status)
        check(payload["reason"] == reason, f"status {status} is diagnosed as {reason}")
        check(phrase in payload["message"], f"{reason} says what actually happened")

    outdated = failures.failure_payload(404)
    check("install-proxy.sh" in outdated["remedy"], "an old proxy is told how to upgrade")
    no_token = failures.failure_payload(503)
    check(
        "BLINK_PROXY_TOKEN" in no_token["remedy"]
        and "systemctl restart" in no_token["remedy"],
        "a tokenless proxy is given the exact provisioning commands",
    )
    for status in cases:
        payload = failures.failure_payload(status)
        check(
            payload["state"] == "failure" and payload["can_submit_pin"] is False,
            f"status {status} produces a renderable failure state",
        )
        check(
            not any(k in payload for k in ("username", "password", "pin", "token")),
            f"status {status} payload carries no credential fields",
        )

    views = (ROOT / "custom_components/blink_liveview_proxy/views.py").read_text()
    check(
        "_safe_auth_error" not in views and "failure_payload" in views,
        "the status view reports the diagnosis instead of a bare 502",
    )
    panel = (ROOT / "custom_components/blink_liveview_proxy/frontend/blink-proxy-auth-panel.js").read_text()
    check('id="recheck"' in panel and "_recheck()" in panel, "the panel offers a re-check button")
    check(
        "cannot run this for you" in panel,
        "the panel says plainly that it cannot apply the fix itself",
    )


def test_proxy_version_notice() -> None:
    print("\nproxy version notice")
    sys.path.insert(0, str(ROOT / "custom_components/blink_liveview_proxy"))
    import version_check

    check(version_check.is_outdated(None, "0.3.0"), "a proxy reporting no version is outdated")
    check(version_check.is_outdated("0.2.0", "0.3.0"), "an older proxy is outdated")
    check(version_check.is_outdated("0.2.9", "0.3.0"), "0.2.9 sorts below 0.3.0, not above")
    check(not version_check.is_outdated("0.3.0", "0.3.0"), "the exact minimum is fine")
    check(not version_check.is_outdated("0.4.1", "0.3.0"), "a newer proxy is never nagged")
    check(not version_check.is_outdated("1.0", "0.3.0"), "a short version still compares")
    check(version_check.is_outdated("garbage", "0.3.0"), "an unparseable version is treated as old")
    check(
        version_check.describe(None) == version_check.UNKNOWN_VERSION
        and version_check.describe("0.2.0") == "0.2.0",
        "the notice can name the version, or say it has none",
    )

    # The released 0.3.0 answers /status without a version field, because the
    # field landed a commit after the tag. Placing it by capability is what
    # keeps that install from being told to upgrade to what it already runs.
    check(
        version_check.infer_version({"auth_state": "success", "ready": True}) == "0.3.0",
        "a versionless proxy with auth routes is placed at 0.3.0",
    )
    check(
        not version_check.is_outdated(
            version_check.infer_version({"auth_state": "idle"}), "0.3.0"
        ),
        "a correct 0.3.0 install is never told it is out of date",
    )
    check(
        version_check.infer_version({"ready": True}) is None
        and version_check.is_outdated(version_check.infer_version({"ready": True}), "0.3.0"),
        "a proxy with neither a version nor the newer fields is still outdated",
    )
    check(
        version_check.infer_version({"version": "0.4.0", "auth_state": "idle"}) == "0.4.0",
        "a reported version always wins over the inference",
    )

    coordinator = (ROOT / "custom_components/blink_liveview_proxy/coordinator.py").read_text()
    check("ir.async_create_issue" in coordinator, "an outdated proxy raises a repair issue")
    check("ir.async_delete_issue" in coordinator, "the notice clears itself once upgraded")
    check(
        "async_get_status" in coordinator,
        "the version is read from /status on the normal poll, not only at setup",
    )

    strings = json.loads((ROOT / "custom_components/blink_liveview_proxy/strings.json").read_text())
    issue = strings.get("issues", {}).get("proxy_outdated", {})
    check(bool(issue.get("title") and issue.get("description")), "the notice has text to show")
    placeholders = set(re.findall(r"\{(\w+)\}", issue.get("description", "") + issue.get("title", "")))
    supplied = set(re.findall(r'"(\w+)": ', coordinator[coordinator.index("translation_placeholders"):]))
    missing = placeholders - supplied
    check(not missing, f"every placeholder in the notice is supplied ({sorted(missing)} missing)")
    check(
        "install-proxy.sh" in issue["description"],
        "the notice says what to run, not just what is wrong",
    )


def test_panel_contract() -> None:
    print("\nauthentication panel contract")
    panel = (ROOT / "custom_components/blink_liveview_proxy/frontend/blink-proxy-auth-panel.js").read_text()
    for banned in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        check(banned not in panel, f"the panel never persists secrets in {banned}")
    check(
        "Bearer" not in panel
        and "Authorization" not in panel
        and "BLINK_PROXY_TOKEN" not in panel,
        "the panel never holds or sends the proxy token itself",
    )
    check("?pin=" not in panel and "?password=" not in panel and "?username=" not in panel, "the panel puts no credential in a query string")
    check(
        'this._api("POST", "login", { username, password })' in panel
        and 'callApi(method, `blink_liveview_proxy/auth/${path}`, body)' in panel,
        "credentials go to Home Assistant in a request body, not a URL",
    )
    check('challenge_id: this._state.challenge_id' in panel, "the PIN is submitted against the live challenge")
    check(panel.count('autocomplete="off"') >= 3, "credential inputs opt out of browser autofill")
    check('type="password"' in panel and 'inputmode="numeric"' in panel, "PIN and password inputs are masked")
    check(
        "this._signature" in panel and "if (signature === this._signature)" in panel,
        "polling does not rebuild the form under a PIN being typed",
    )
    check(
        'getElementById("expires")' in panel and "node.textContent" in panel,
        "the expiry countdown updates without a rebuild",
    )
    check(
        'usernameInput.value = ""' in panel and 'input.value = ""' in panel,
        "credential fields are cleared as soon as they are submitted",
    )


async def async_main() -> None:
    await test_controller()
    await test_routes()
    await test_existing_routes_unchanged()
    await test_restart_safety()
    await test_blink_client_persistence()
    test_home_assistant_route_authorization()
    test_failure_classification()
    test_proxy_version_notice()
    test_panel_contract()
    test_compatibility_assets()


def main() -> int:
    asyncio.run(async_main())
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for name in FAILURES:
            print(f"  {name}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
