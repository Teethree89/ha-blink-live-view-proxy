"""A disposable installation of the proxy, separate from the serving instance.

Each run has its own source copy, virtualenv, API token and state directory.
It never copies a Blink session or inherits login/Supervisor credentials. The
worker listens on loopback; callers reach an allow-listed API through the
parent, so neither its port nor its token has to leave this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import secrets
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .constants import APP_ROOT, DEFAULT_CONFIG, PROXY_VERSION
from .selfupdate import detect_method

TEST_TTL = 30 * 60
BUILD_TIMEOUT = 10 * 60
REUSABLE_KEYS = frozenset({
    "cameras", "ffmpeg", "ffmpeg_probesize", "ffmpeg_analyzeduration",
    "hls_idle_timeout", "hls_start_timeout", "mpegts_session_seconds",
    "ptt_disabled_product_types", "ptt_disabled_camera_types", "ptt_force_enabled_slugs",
})
ALLOWED_REQUESTS = frozenset({
    ("GET", "health"), ("GET", "status"), ("GET", "auth/status"),
    ("GET", "cameras"), ("POST", "auth/login"), ("POST", "auth/pin"),
    ("POST", "auth/cancel"),
})


def isolated_config(current: dict[str, Any], root: Path, reuse: bool) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["cameras"] = {}
    if reuse:
        config.update({key: copy.deepcopy(current[key]) for key in REUSABLE_KEYS if key in current})
    config.update({
        "host": "127.0.0.1", "port": 0,
        "auth_file": str(root / "state/blink-auth.json"),
        "hls_dir": str(root / "state/hls"),
        "liveview_cache_dir": str(root / "state/liveviews"),
        "clip_cache_dir": str(root / "state/clips"),
        "clip_cache_max_mb": 8, "save_liveview_cache": False,
        "_installation_test": True,
    })
    return config


def isolated_environment(root: Path, token: str) -> dict[str, str]:
    allowed = {
        "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update({
        "HOME": str(root), "PYTHONUNBUFFERED": "1",
        "BLINK_PROXY_TOKEN": token, "BLINK_INSTALLATION_TEST_CHILD": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_INDEX_URL": "https://pypi.org/simple",
    })
    return env


class InstallationTest:
    """Own one bounded test; shutdown and expiry use the same cleanup path."""

    def __init__(self, config: dict[str, Any], *, source: Path = APP_ROOT, ttl: float = TEST_TTL):
        self.config = config
        self.source = source
        self.ttl = ttl
        self.test_id: str | None = None
        self.root: Path | None = None
        self.phase = "idle"
        self.checks: list[str] = []
        self.error: str | None = None
        self.reuse = False
        self.deadline = 0.0
        self._token = ""
        self._url = ""
        self._process: asyncio.subprocess.Process | None = None
        self._build_task: asyncio.Task | None = None
        self._expiry_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return {
            "id": self.test_id, "phase": self.phase, "error": self.error,
            "checks": list(self.checks), "reuse_settings": self.reuse,
            "proxy_installation": detect_method(), "source_version": PROXY_VERSION,
            "expires_in": max(0, int(self.deadline - time.monotonic())),
            "coverage": "Fresh Python environment and proxy runtime; native add-on/container/service provisioning is not exercised.",
        }

    async def start(self, reuse: bool) -> dict[str, Any]:
        async with self._lock:
            if self.root is not None:
                raise RuntimeError("A test already exists; discard it before starting another")
            self.root = Path(tempfile.mkdtemp(prefix="blink-install-test-"))
            self.test_id = secrets.token_urlsafe(24)
            self._token = secrets.token_hex(32)
            self.phase, self.error, self.checks = "preparing", None, []
            self.reuse = reuse
            self.deadline = time.monotonic() + self.ttl
            self._build_task = asyncio.create_task(self._build())
            self._expiry_task = asyncio.create_task(self._expire())
            return self.status()

    async def _command(self, *args: str) -> None:
        assert self.root is not None
        with (self.root / "install.log").open("ab") as output:
            await self._spawn(
                *args, cwd=self.root, env=isolated_environment(self.root, self._token),
                stdout=output, stderr=output, start_new_session=True,
            )
            if await self._process.wait() != 0:
                raise RuntimeError("Installation command failed")
        self._process = None

    async def _spawn(self, *args: str, **kwargs: Any) -> None:
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
        try:
            self._process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Spawning is not atomic with cancellation: retain the PID even
            # when Cancel lands while the OS is creating the process.
            self._process = await spawning
            raise

    def _prepare(self) -> Path:
        assert self.root is not None
        target = self.root / "proxy"
        target.mkdir()
        shutil.copytree(self.source / "blink_proxy", target / "blink_proxy",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(self.source / "requirements.txt", target / "requirements.txt")
        config = isolated_config(self.config, self.root, self.reuse)
        path = self.root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        path.chmod(0o600)
        return target

    async def _build(self) -> None:
        try:
            async with asyncio.timeout(BUILD_TIMEOUT):
                preparation = asyncio.create_task(asyncio.to_thread(self._prepare))
                try:
                    target = await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    # A worker thread cannot be cancelled. Let it finish before
                    # discard removes its directory, so it cannot recreate it.
                    await preparation
                    raise
                self.checks.append("isolated_source_and_config")
                assert self.root is not None
                python = self.root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                self.phase = "installing_dependencies"
                await self._command(sys.executable, "-m", "venv", str(self.root / "venv"))
                await self._command(str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                    "--no-cache-dir", "-r", str(target / "requirements.txt"))
                self.checks.append("fresh_dependency_install")
                self.phase = "starting"
                with (self.root / "worker.log").open("ab") as output:
                    await self._spawn(
                        str(python), "-m", "blink_proxy.installation_worker", str(self.root),
                        cwd=target, env=isolated_environment(self.root, self._token),
                        stdout=output, stderr=output, start_new_session=True,
                    )
                ready = self.root / "ready.json"
                for _ in range(120):
                    if self._process.returncode is not None:
                        raise RuntimeError("Temporary proxy failed to start")
                    if await asyncio.to_thread(ready.exists):
                        data = json.loads(await asyncio.to_thread(ready.read_text))
                        self._url = f"http://127.0.0.1:{int(data['port'])}"
                        break
                    await asyncio.sleep(0.25)
                else:
                    raise TimeoutError("Temporary proxy did not become ready")
                self.checks.append("separate_proxy_started")
                await self.request("GET", "health")
                await self.request("GET", "auth/status")
                self.checks.extend(["health", "generated_token_authenticated"])
                self.phase = "ready"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - subprocess output can contain sensitive paths
            failed_phase = self.phase
            self.error = f"{failed_phase}_failed"
            self.phase = "failed"
            await self._stop_process()

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if (method, path) not in ALLOWED_REQUESTS or not self._url:
            raise ValueError("Temporary proxy action is unavailable")
        async with ClientSession(timeout=ClientTimeout(total=20)) as session:
            async with session.request(method, self._url + "/" + path, json=body,
                                       headers={"Authorization": f"Bearer {self._token}"}) as response:
                response.raise_for_status()
                return await response.json()

    async def _stop_process(self) -> None:
        process = self._process
        if process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        self._process = None

    async def discard(self) -> dict[str, Any]:
        async with self._lock:
            if self._expiry_task is not None and self._expiry_task is not asyncio.current_task():
                self._expiry_task.cancel()
            if self._build_task is not None and not self._build_task.done():
                self._build_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._build_task
            await self._stop_process()
            if self.root is not None:
                await asyncio.to_thread(shutil.rmtree, self.root)
            self.root = None
            self._token, self._url = "", ""
            self.phase = "discarded"
            return self.status()

    async def _expire(self) -> None:
        await asyncio.sleep(self.ttl)
        await self.discard()
