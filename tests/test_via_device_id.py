"""The device tree must not use the deprecated `via_device` parameter.

No Home Assistant, no network. The modules are read by source and parsed, not
imported, because importing the package pulls in Home Assistant. Run from the
repo root:

    python tests/test_via_device_id.py

Home Assistant 2026.9 logs a warning for `via_device` in device_info and says
it stops working in 2027.8. The replacement, `via_device_id`, does not take an
identifier tuple - it takes a device registry id - so swapping the key alone
would hand the registry a tuple and silently lose the parent link.

That is why `__init__.py` registers the proxy device itself and stashes its id
before forwarding the platforms. Two things can quietly undo it, and this test
guards both:

  1. `via_device` creeping back into a device_info dict anywhere.
  2. `hub_device_id` being written after `async_forward_entry_setups`, which
     would hand the camera platform a KeyError. PLATFORMS lists CAMERA first,
     so on a fresh install nothing else creates the parent in time.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components/blink_liveview_proxy"

FAILURES: list[str] = []


def fail(message: str) -> None:
    FAILURES.append(message)


def check_no_via_device() -> None:
    """No source file may pass `via_device` to the device registry."""
    for path in sorted(COMPONENT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # device_info written as a dict literal
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value == "via_device":
                        fail(
                            f"{path.name}:{key.lineno} uses the deprecated "
                            "`via_device`; use `via_device_id` with a device "
                            "registry id"
                        )
            # or passed as a keyword, e.g. DeviceInfo(via_device=...)
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "via_device":
                        fail(
                            f"{path.name}:{node.lineno} passes the deprecated "
                            "`via_device=` keyword; use `via_device_id=`"
                        )


def check_hub_registered_before_platforms() -> None:
    """`hub_device_id` must exist before the platforms are forwarded."""
    path = COMPONENT / "__init__.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    stash_line: int | None = None
    forward_line: int | None = None

    for node in ast.walk(tree):
        # hass.data[DOMAIN][entry.entry_id]["hub_device_id"] = hub.id
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "hub_device_id":
                if stash_line is None or node.lineno < stash_line:
                    stash_line = node.lineno
        if isinstance(node, ast.Attribute) and node.attr == "async_forward_entry_setups":
            if forward_line is None or node.lineno < forward_line:
                forward_line = node.lineno

    if stash_line is None:
        fail(
            "__init__.py never stores `hub_device_id`; the camera platform "
            "needs it to build `via_device_id`"
        )
        return
    if forward_line is None:
        fail("__init__.py no longer calls async_forward_entry_setups")
        return
    if stash_line > forward_line:
        fail(
            f"__init__.py stores `hub_device_id` on line {stash_line}, after "
            f"async_forward_entry_setups on line {forward_line}; the camera "
            "platform runs first and would raise KeyError"
        )


def check_camera_reads_hub_id() -> None:
    """camera.py must actually use the stashed id."""
    path = COMPONENT / "camera.py"
    source = path.read_text(encoding="utf-8")
    if "hub_device_id" not in source:
        fail("camera.py does not read `hub_device_id`; the parent link is lost")
    if '"via_device_id"' not in source and "via_device_id=" not in source:
        fail("camera.py sets no `via_device_id`; cameras would sit at the root")


def main() -> int:
    check_no_via_device()
    check_hub_registered_before_platforms()
    check_camera_reads_hub_id()

    if FAILURES:
        for line in FAILURES:
            print(f"FAIL: {line}")
        return 1
    print("ok: no `via_device`, hub id stashed before the platforms are forwarded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
