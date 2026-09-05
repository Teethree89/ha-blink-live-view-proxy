"""A saved HLS live view is the one just watched, not an older file.

The MPEG-TS path deletes the previous cached file and repoints last_liveviews at
the new one when a session ends. The HLS path finalizes a cached copy too, and
without the same two steps find_last_liveview kept returning the first recording
it ever saw: Save handed back a stale clip on every session after the first.
Run from the repo root:

    python tests/test_hls_save.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy.hls import HlsSession  # noqa: E402
from blink_proxy.liveview_cache import find_last_liveview  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


class _StubManager:
    """Just the pieces HlsSession touches for finalizing a cached copy."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root_dir = root
        self.last_liveviews: dict[str, dict] = {}


def _finalize(session: HlsSession, root: pathlib.Path, name: str) -> pathlib.Path:
    final = root / name
    tmp = final.with_suffix(".ts.part")
    tmp.write_bytes(b"MPEGTS-BYTES")
    session.cache_final = final
    session.cache_tmp = tmp
    session._finalize_cache()
    return final


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        manager = _StubManager(root)
        slug = "flood_light"
        session = HlsSession(slug, manager)
        # What Save consults: the same map the manager holds, plus the cache dir.
        app = {"last_liveviews": manager.last_liveviews, "liveview_cache_dir": root}

        first = _finalize(session, root, "20260101_100000_flood-light_liveview.ts")
        check(first.exists(), "the first session's file is published")
        found = find_last_liveview(app, slug) or {}
        check(found.get("path") == str(first), "Save finds the first recording")

        second = _finalize(session, root, "20260101_100500_flood-light_liveview.ts")
        found = find_last_liveview(app, slug) or {}
        check(
            found.get("path") == str(second),
            "Save now finds the second recording, not the first",
        )
        check(not first.exists(), "the first recording was removed")
        check(second.exists(), "the second recording is the one on disk")

        # An old file that cannot be removed is a warning at most, never a lost recording.
        stubborn = root / "stubborn"
        stubborn.mkdir()
        manager.last_liveviews[slug] = {"path": str(stubborn)}
        third = _finalize(session, root, "20260101_101000_flood-light_liveview.ts")
        found = find_last_liveview(app, slug) or {}
        check(third.exists(), "the third recording is published past an undeletable previous one")
        check(found.get("path") == str(third), "and Save finds it")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
