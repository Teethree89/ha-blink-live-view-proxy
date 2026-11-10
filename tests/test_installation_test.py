"""Installation-test isolation and lifecycle; never contacts Blink or pip.

The worker runs the real copied proxy against the dependencies installed for
this test suite. The fresh pip install is covered by a separate manual smoke
test, rather than downloading dependencies on every CI run.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy.constants import DEFAULT_CONFIG
from blink_proxy.installation_test import InstallationTest, isolated_config, isolated_environment
from blink_proxy.routes import make_app


class InstalledDependenciesTest(InstallationTest):
    """Exercise a real worker without running pip in automated tests."""

    async def _command(self, *args):
        pass

    async def _spawn(self, *args, **kwargs):
        if "blink_proxy.installation_worker" in args:
            args = (sys.executable, *args[1:])
        await super()._spawn(*args, **kwargs)


class IsolationTests(unittest.TestCase):
    def test_configuration_reuses_only_selected_inputs(self):
        original = copy.deepcopy(DEFAULT_CONFIG)
        original.update({
            "auth_file": "/production/session.json", "proxy_token": "secret",
            "host": "0.0.0.0", "port": 8088,
            "hls_dir": "/production/hls", "clip_cache_dir": "/production/clips",
            "cameras": {"door": {"name": "Door"}}, "username": "private@example.com",
        })
        frozen = copy.deepcopy(original)
        root = Path("/temporary-test")
        config = isolated_config(original, root, True)
        for key in ("auth_file", "hls_dir", "liveview_cache_dir", "clip_cache_dir"):
            self.assertTrue(Path(config[key]).is_relative_to(root))
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["port"], 0)
        self.assertNotIn("proxy_token", config)
        self.assertNotIn("username", config)
        config["cameras"]["door"]["name"] = "changed"
        self.assertEqual(original, frozen)
        self.assertEqual(isolated_config(original, root, False)["cameras"], {})

    def test_child_does_not_inherit_credentials_or_supervisor_access(self):
        with patch.dict(os.environ, {
            "BLINK_USERNAME": "account", "BLINK_PASSWORD": "password",
            "BLINK_2FA_CODE": "123456", "SUPERVISOR_TOKEN": "supervisor-secret",
            "CUSTOM_PASSWORD": "custom-secret", "PYTHONPATH": "/production",
            "PIP_EXTRA_INDEX_URL": "https://private.example/secret",
        }):
            env = isolated_environment(Path("/test"), "new-token")
        for key in ("BLINK_USERNAME", "BLINK_PASSWORD", "BLINK_2FA_CODE", "SUPERVISOR_TOKEN",
                    "CUSTOM_PASSWORD", "PYTHONPATH", "PIP_EXTRA_INDEX_URL"):
            self.assertNotIn(key, env)
        self.assertEqual(env["BLINK_PROXY_TOKEN"], "new-token")
        self.assertEqual(env["HOME"], "/test")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test = InstalledDependenciesTest({}, ttl=60)

    async def asyncTearDown(self):
        await self.test.discard()

    async def wait_ready(self):
        for _ in range(200):
            if self.test.phase in {"ready", "failed"}:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.test.phase, "ready", self.test.status())

    async def test_real_worker_is_separate_and_never_starts_blink_login(self):
        await self.test.start(True)
        root = self.test.root
        with self.assertRaises(RuntimeError):
            await self.test.start(False)
        await self.wait_ready()
        self.assertTrue((await self.test.request("GET", "health"))["ok"])
        auth = await self.test.request("GET", "auth/status")
        self.assertEqual(auth["state"], "idle")
        self.assertFalse(auth["authenticated"])
        self.assertFalse((root / "state/blink-auth.json").exists())
        for method, path in (("POST", "auth/reset"), ("POST", "update"),
                             ("GET", "devices"), ("POST", "../auth/login")):
            with self.assertRaises(ValueError):
                await self.test.request(method, path)
        private = self.test._token
        self.assertNotIn(private, json.dumps(self.test.status()))
        process = self.test._process
        result = await self.test.discard()
        self.assertEqual(result["phase"], "discarded")
        self.assertFalse(root.exists())
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.test._token, "")
        self.assertIn("health", result["checks"])
        await self.test.discard()  # Repeated cleanup is safe.

    async def test_expiry_discards_an_abandoned_install(self):
        self.test.ttl = 0.05
        async def slow_build():
            await asyncio.sleep(30)

        with patch.object(self.test, "_build", side_effect=slow_build):
            await self.test.start(False)
            root = self.test.root
            await asyncio.sleep(0.2)
        self.assertEqual(self.test.phase, "discarded")
        self.assertEqual(self.test.status()["cleanup_reason"], "expired")
        self.assertFalse(root.exists())

    async def test_cancel_kills_an_installation_subprocess(self):
        async def sleeping_install():
            # The fixture stubs _command out to skip pip; this test needs the real one.
            await InstallationTest._command(self.test, sys.executable, "-c", "import time; time.sleep(30)")

        with patch.object(self.test, "_build", side_effect=sleeping_install):
            await self.test.start(False)
            for _ in range(100):
                if self.test._process is not None:
                    break
                await asyncio.sleep(0.01)
            process = self.test._process
            self.assertIsNotNone(process)
            root = self.test.root
            await self.test.discard()
        self.assertIsNotNone(process.returncode)
        self.assertFalse(root.exists())

    async def test_installation_failure_is_reported_and_can_be_discarded(self):
        with patch.object(self.test, "_command", side_effect=RuntimeError("private error")):
            await self.test.start(False)
            root = self.test.root
            for _ in range(100):
                if self.test.phase == "failed":
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(self.test.phase, "failed")
        self.assertNotIn("private error", json.dumps(self.test.status()))
        for _ in range(100):
            if self.test.root is None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNone(self.test.root)
        self.assertFalse(root.exists())
        self.assertEqual(self.test._token, "")
        self.assertEqual(self.test.status()["cleanup_reason"], "failed")
        await self.test.discard()
        self.assertFalse(root.exists())

    async def test_failure_kills_a_partial_build_process_and_removes_data(self):
        processes = []

        async def broken_build(*args):
            await self.test._spawn(sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True)
            processes.append(self.test._process)
            raise RuntimeError("private build diagnostic")

        with patch.object(self.test, "_command", side_effect=broken_build):
            await self.test.start(False)
            root = self.test.root
            await self.test._build_task
        self.assertEqual(self.test.phase, "failed")
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(root.exists())
        self.assertEqual(self.test._token, "")
        self.assertNotIn("private build diagnostic", json.dumps(self.test.status()))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.login = patch("blink_proxy.routes.AuthenticationController.start_startup_login", new_callable=AsyncMock)
        self.startup = self.login.start()
        self.environment = patch.dict(os.environ, {"BLINK_PROXY_TOKEN": "parent-token"})
        self.environment.start()
        config = isolated_config({}, Path(self.directory.name), False)
        config.pop("_installation_test")
        self.app = await make_app(config, Path(self.directory.name), None)
        self.original_client = self.app["client"]
        self.manager = self.app["installation_test"]
        self.start = patch.object(self.manager, "start", new_callable=AsyncMock)
        self.start_mock = self.start.start()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.start.stop()
        self.environment.stop()
        self.login.stop()
        self.directory.cleanup()

    async def test_management_requires_header_auth_and_valid_inputs(self):
        for headers, query, status in (({}, "", 401), ({}, "?token=parent-token", 401),
                                       ({"Authorization": "Bearer wrong"}, "", 401)):
            response = await self.client.post("/installation-test" + query,
                                              json={"reuse_settings": True}, headers=headers)
            self.assertEqual(response.status, status)
        self.start_mock.assert_not_called()
        headers = {"Authorization": "Bearer parent-token"}
        response = await self.client.post("/installation-test", json={"reuse_settings": "yes"}, headers=headers)
        self.assertEqual(response.status, 400)
        self.start_mock.return_value = {"phase": "preparing"}
        response = await self.client.post("/installation-test", json={"reuse_settings": True}, headers=headers)
        self.assertEqual(response.status, 200)
        self.start_mock.assert_awaited_once_with(True)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_stale_run_cannot_reach_or_discard_current_run(self):
        self.manager.test_id = "current"
        self.manager.phase = "ready"
        headers = {"Authorization": "Bearer parent-token"}
        with patch.object(self.manager, "request", new_callable=AsyncMock) as forward:
            response = await self.client.get("/installation-test/old/proxy/auth/status", headers=headers)
            self.assertEqual(response.status, 404)
            forward.assert_not_called()
        response = await self.client.delete("/installation-test?id=old", headers=headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.manager.phase, "ready")
        self.assertIs(self.app["client"], self.original_client)


if __name__ == "__main__":
    unittest.main()
