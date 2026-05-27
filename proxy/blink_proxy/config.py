"""Configuration and file helpers."""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import tempfile
from pathlib import Path
from typing import Any

import certifi
from aiohttp import ClientSession, TCPConnector

from .constants import APP_ROOT, DEFAULT_CONFIG

def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge dictionaries without mutating inputs."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def load_config(path: Path | None) -> tuple[dict[str, Any], Path]:
    """Load JSON config and return the config plus relative path base."""
    if path is None:
        env_path = os.getenv("BLINK_PROXY_CONFIG", "")
        path = Path(env_path) if env_path else APP_ROOT / "config.json"
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return deep_merge(DEFAULT_CONFIG, json.load(handle)), path.parent
    return dict(DEFAULT_CONFIG), APP_ROOT

def resolve_path(value: str | os.PathLike[str], base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path

def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)

def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)

def create_client_session() -> ClientSession:
    """Create an aiohttp session with certifi roots for macOS Python builds."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return ClientSession(connector=TCPConnector(ssl=ssl_context))
