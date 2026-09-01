"""Tests for the login decisions that have cost people a working install.

No Home Assistant, no Blink account, no network, no blinkpy. The functions
under test are loaded out of blink_proxy/blink.py by source, because importing
the module pulls in blinkpy. Run from the repo root:

    python tests/test_login_decisions.py

Each function here is small on purpose. They were pulled out of
``BlinkClient.start()``, where they sat between blinkpy calls and could not be
asserted on at all — which is how a stale 2FA code came to skip the wait
entirely and lock users out, without a test being able to see it.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "proxy/blink_proxy/blink.py"

# The functions log warnings on the paths under test; the assertions cover the
# text, so keep the run output readable.
logging.getLogger("test").setLevel(logging.CRITICAL)

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def load(*names: str) -> dict:
    """Pull named top-level functions out of blink.py without importing it."""
    tree = ast.parse(SOURCE.read_text())
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    missing = set(names) - set(wanted)
    if missing:
        sys.exit(f"not found in {SOURCE.name}: {', '.join(sorted(missing))}")

    module = ast.Module(body=[wanted[name] for name in names], type_ignores=[])
    namespace: dict = {
        "uuid": uuid,
        "Any": object,
        "LOGGER": logging.getLogger("test"),
        "normalize_slug": lambda value: str(value).strip().lower().replace(" ", "_"),
    }
    exec(compile(ast.fix_missing_locations(module), "blink.py", "exec"), namespace)
    return namespace


NS = load("plan_2fa_code", "_discard_bad_hardware_id", "camera_ptt_supported")


def test_plan_2fa_code() -> None:
    """A code present before the challenge cannot belong to it."""
    print("\nplan_2fa_code")
    plan = NS["plan_2fa_code"]

    action, warning = plan(None, "", interactive=False)
    check(action == "wait", "no code, no tty -> wait")
    check(warning is None, "no code -> no warning")

    action, warning = plan(None, "", interactive=True)
    check(action == "prompt", "no code, tty -> prompt")

    # The regression. A stale value used to make `code` truthy, which skipped
    # the wait, submitted the old code, and locked the user out.
    action, warning = plan(None, "123456", interactive=False)
    check(action == "wait", "stale env code still waits, never submits")
    check(warning is not None, "stale env code warns")
    check(
        warning is not None and "environment variable" in warning,
        "warning names the environment variable",
    )

    action, warning = plan("654321", "", interactive=False)
    check(action == "wait", "stale --pin still waits")
    check(warning is not None and "--pin" in warning, "warning names --pin")

    action, warning = plan("654321", "123456", interactive=False)
    check(
        warning is not None and "--pin" in warning and "environment" in warning,
        "both sources named when both are set",
    )

    # A tty means a human can answer during the challenge, so prompting is
    # still right — but the stale value is reported either way.
    action, warning = plan(None, "123456", interactive=True)
    check(action == "prompt", "stale code with a tty still prompts")
    check(warning is not None, "stale code warns even when prompting")


def test_discard_bad_hardware_id() -> None:
    """Blink 406s a non-UUID hardware_id before login is even attempted."""
    print("\n_discard_bad_hardware_id")
    discard = NS["_discard_bad_hardware_id"]

    for value, keep, label in [
        (str(uuid.uuid4()).upper(), True, "uppercase UUID kept"),
        (str(uuid.uuid4()), True, "lowercase UUID kept"),
        ("Home Assistant", False, "the real-world bad value dropped"),
        ("", False, "empty string dropped"),
        (12345, False, "non-string dropped"),
        ("not-a-uuid", False, "arbitrary text dropped"),
    ]:
        data = {"hardware_id": value}
        discard(data)
        check(("hardware_id" in data) is keep, label)

    data: dict = {}
    discard(data)
    check("hardware_id" not in data, "absent key is left absent")

    data = {"hardware_id": str(uuid.uuid4()), "username": "someone"}
    discard(data)
    check(data.get("username") == "someone", "other keys untouched")


def test_camera_ptt_supported() -> None:
    """Mini/owl cameras are off by default but can be opted back in."""
    print("\ncamera_ptt_supported")
    supported = NS["camera_ptt_supported"]

    class Camera:
        def __init__(self, camera_type="default", product_type="catalina"):
            self.camera_type = camera_type
            self.product_type = product_type

    config = {
        "ptt_disabled_camera_types": ["mini"],
        "ptt_disabled_product_types": ["owl"],
        "ptt_force_enabled_slugs": [],
    }

    check(supported(Camera(), config), "a regular camera is supported")
    check(
        not supported(Camera("mini", "owl"), config),
        "a Mini/owl is disabled by default",
    )
    check(
        not supported(Camera("MINI", "OWL"), config),
        "the family match is case-insensitive",
    )

    forced = dict(config, ptt_force_enabled_slugs=["kitchen"])
    check(
        supported(Camera("mini", "owl"), forced, slug="kitchen"),
        "a forced slug overrides the family default",
    )
    check(
        not supported(Camera("mini", "owl"), forced, slug="hallway"),
        "forcing one slug does not force the others",
    )
    check(
        supported(Camera("mini", "owl"), forced, slug="Kitchen"),
        "the forced slug is normalised before matching",
    )

    check(supported(Camera(), {}), "an empty config disables nothing")


def main() -> int:
    for test in (
        test_plan_2fa_code,
        test_discard_bad_hardware_id,
        test_camera_ptt_supported,
    ):
        test()

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
