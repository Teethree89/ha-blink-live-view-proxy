"""Fixtures for the tests that run the integration inside real Home Assistant.

These need pytest-homeassistant-custom-component, which pins a Home Assistant
release, plus blinkpy for the proxy side. The other tests in tests/ need
neither. From the repo root:

    pip install pytest-homeassistant-custom-component PyTurboJPEG
    pip install --no-deps blinkpy==0.25.9
    pytest tests/ha -o asyncio_mode=auto

The proxy here is the real one - its routes and device module - serving a
blinkpy session with no network behind it (tests/blink_fakes.py). So a switch
turned off in Home Assistant goes through the integration's client, the
proxy's route and handler, and ends as a call on the blinkpy camera.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "proxy", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from blink_fakes import FakeBlink  # noqa: E402
from blink_proxy.blink import BlinkClient  # noqa: E402
from blink_proxy.devices import DevicePoller  # noqa: E402
from blink_proxy.routes import (  # noqa: E402
    cameras_handler,
    devices_handler,
    health_handler,
    motion_detection_handler,
    snapshot_handler,
    snapshot_refresh_handler,
    status_handler,
    sync_arm_handler,
)

PROXY_TOKEN = "proxy-test-token"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Load custom_components/ from this repository."""


class FakeProxy:
    """The real proxy routes around a FakeBlink, and a log of what was asked."""

    def __init__(self, tmp: str) -> None:
        self.blink = FakeBlink()
        config = {
            "auth_file": str(pathlib.Path(tmp) / "auth.json"),
            "cameras": {
                "driveway": {"id": "909058", "serial": "SERIAL-A",
                             "entity_id": "camera.driveway"},
                "back_door": {"id": "1022965", "serial": "SERIAL-B",
                              "entity_id": "camera.back_door"},
            },
        }
        client = BlinkClient(config, pathlib.Path(tmp), None)
        client.blink = self.blink
        client.ready = True
        self.requests: list[str] = []

        @web.middleware
        async def record(request: web.Request, handler: Any) -> web.StreamResponse:
            self.requests.append(f"{request.method} {request.path}")
            return await handler(request)

        app = web.Application(middlewares=[record])
        app["client"] = client
        app["config"] = config
        app["proxy_token"] = PROXY_TOKEN

        class Controller:
            state = "success"

        app["auth_controller"] = Controller()
        app["device_poller"] = DevicePoller(lambda: app["client"])
        app.router.add_get("/health", health_handler)
        app.router.add_get("/status", status_handler)
        app.router.add_get("/cameras", cameras_handler)
        app.router.add_get("/devices", devices_handler)
        app.router.add_get("/cameras/{slug}/snapshot.jpg", snapshot_handler)
        app.router.add_post("/cameras/{slug}/snapshot", snapshot_refresh_handler)
        app.router.add_post("/cameras/{slug}/motion", motion_detection_handler)
        app.router.add_post("/sync/{network_id}/arm", sync_arm_handler)
        self.app = app
        self.server = TestServer(app, host="127.0.0.1")

    @property
    def base_url(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    def asked(self, path: str) -> int:
        return sum(1 for item in self.requests if item.split(" ", 1)[1] == path)


@pytest.fixture
async def fake_proxy(socket_enabled: Any) -> Any:
    with tempfile.TemporaryDirectory() as tmp:
        proxy = FakeProxy(tmp)
        await proxy.server.start_server()
        try:
            yield proxy
        finally:
            await proxy.app["device_poller"].close()
            await proxy.server.close()
