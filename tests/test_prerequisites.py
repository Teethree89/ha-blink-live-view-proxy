"""Checks on the dashboard's prerequisite readout.

No Home Assistant, no network. The module is imported directly, which is why
it carries no Home Assistant imports of its own. Run from the repo root:

    python tests/test_prerequisites.py

The rule being defended everywhere below is that "cannot be checked" is not
"broken". A proxy too old to report its environment, and a Lovelace that has
not started, are both ordinary states of a working install, and a readout that
painted them red would send people to repair something that was already right.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components/blink_liveview_proxy"
sys.path.insert(0, str(COMPONENT))

from prerequisites import MISSING, OK, UNKNOWN, build, summarize  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0

RESOURCE_URL = "/api/blink_liveview_proxy/assets/blink-liveview-dialog.js"
LEGACY_RESOURCE_URL = "/api/blink_liveview_proxy/static/blink-liveview-dialog.js"


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def facts(**overrides) -> dict:
    """A fully healthy install, so each test can break exactly one thing."""
    base = {
        "ha_version": "2026.9.0",
        "minimum_ha": "2024.6.0",
        "required_blinkpy": "0.25.9",
        "environment_proxy_version": "0.6.1",
        "proxy_version": "0.6.1",
        "environment": {
            "python": "3.12.3",
            "blinkpy": "0.25.9",
            "ffmpeg": "/usr/bin/ffmpeg",
        },
        "resource_url": RESOURCE_URL,
        "legacy_resource_url": LEGACY_RESOURCE_URL,
        "integration_version": "0.6.2",
        "hacs_update": {
            "found": True,
            "update_available": False,
            "installed": "0.6.2",
            "latest": "0.6.2",
        },
        "resource_urls": [
            RESOURCE_URL,
            "/hacsfiles/button-card/button-card.js",
            "/hacsfiles/lovelace-auto-entities/auto-entities.js",
        ],
        "lovelace_mode": "storage",
        "blink_entries": 1,
        "blink_loaded": 1,
        "blink_service": True,
    }
    base.update(overrides)
    return base


def rows(**overrides) -> dict[str, dict]:
    return {row["key"]: row for row in build(facts(**overrides))}


def test_shape() -> None:
    print("\nevery row is renderable")
    keys = {
        "home_assistant",
        "integration_update",
        "blink_integration",
        "blinkpy",
        "ffmpeg",
        "dashboard_resource",
        "button_card",
        "auto_entities",
    }
    result = build(facts())
    check(set(row["key"] for row in result) == keys, "all eight checks are present")
    check(
        len({row["key"] for row in result}) == len(result),
        "no two checks share a key",
    )
    for row in result:
        check(
            all(
                isinstance(row.get(field), str) and row[field]
                for field in ("label", "state", "detail", "needed_for")
            ),
            f"{row['key']} has text in every displayed field",
        )
        # The point of the whole design: instructions belong to the check, not
        # to its failure, so a green install can still read how it was built.
        check(
            bool(row["instructions"]) and all(row["instructions"]),
            f"{row['key']} carries instructions even when it passes",
        )
        check(row["state"] in {OK, MISSING, UNKNOWN}, f"{row['key']} has a known state")


def test_all_green() -> None:
    print("\na healthy install reports nothing to do")
    result = build(facts())
    check(all(row["state"] == OK for row in result), "every check passes")
    summary = summarize(result)
    check(summary == {"total": 8, "ok": 8, "missing": 0, "unknown": 0, "blocking": 0},
          f"the summary counts them all as ready ({summary})")


def test_home_assistant() -> None:
    print("\nHome Assistant version")
    check(rows(ha_version="2024.6.0")["home_assistant"]["state"] == OK,
          "the floor itself passes")
    check(rows(ha_version="2024.5.5")["home_assistant"]["state"] == MISSING,
          "one release below the floor fails")
    check(rows(ha_version="2026.10.0b3")["home_assistant"]["state"] == UNKNOWN,
          "a beta is not comparable, and is not called a failure")
    check(rows(ha_version="")["home_assistant"]["state"] == UNKNOWN,
          "an unreported core version is not called a failure")


def test_blink_integration() -> None:
    print("\nofficial Blink integration")
    absent = rows(blink_entries=0, blink_loaded=0, blink_service=False)
    check(absent["blink_integration"]["state"] == MISSING, "absence is reported")
    check(absent["blink_integration"]["required"] is False,
          "but it is never marked required")
    check("without it" in absent["blink_integration"]["detail"],
          "and the detail says what still works")

    retrying = rows(blink_entries=1, blink_loaded=0, blink_service=False)
    check("not loaded" in retrying["blink_integration"]["detail"],
          "an entry stuck in setup_retry is distinguished from absence")

    no_service = rows(blink_service=False)
    check("trigger_camera" in no_service["blink_integration"]["detail"],
          "a loaded entry without the action names the action")

    check(summarize(build(facts(blink_entries=0, blink_loaded=0,
                               blink_service=False)))["blocking"] == 0,
          "an optional check missing never counts as blocking")


def test_blinkpy() -> None:
    print("\nblinkpy on the proxy")
    check(rows()["blinkpy"]["state"] == OK, "the pinned version passes")
    wrong = rows(environment={"blinkpy": "0.25.5", "ffmpeg": "/usr/bin/ffmpeg"})
    check(wrong["blinkpy"]["state"] == MISSING, "a different version fails")
    check("0.25.5" in wrong["blinkpy"]["detail"] and "0.25.9" in wrong["blinkpy"]["detail"],
          "and the detail names both versions")
    absent = rows(environment={"blinkpy": None, "ffmpeg": "/usr/bin/ffmpeg"})
    check(absent["blinkpy"]["state"] == MISSING, "no blinkpy at all fails")


def test_ffmpeg() -> None:
    print("\nffmpeg on the proxy")
    check(rows()["ffmpeg"]["detail"] == "/usr/bin/ffmpeg",
          "a resolved binary is reported by path")
    missing = rows(environment={"blinkpy": "0.25.9", "ffmpeg": None})
    check(missing["ffmpeg"]["state"] == MISSING, "an unresolvable binary fails")


def test_old_proxy_is_not_a_failure() -> None:
    print("\na proxy too old to answer")
    old = rows(environment=None, proxy_version="0.6.0")
    for key in ("blinkpy", "ffmpeg"):
        check(old[key]["state"] == UNKNOWN, f"{key} is unknown, not missing")
        check("0.6.1" in old[key]["detail"] and "0.6.0" in old[key]["detail"],
              f"{key} names the release that added the report, and the one running")
    check(summarize(build(facts(environment=None)))["blocking"] == 0,
          "unknown never counts as blocking")

    silent = rows(environment=None, proxy_version=None)
    check("did not say" in silent["blinkpy"]["detail"],
          "a proxy that reports no version at all is described honestly")


def test_integration_update() -> None:
    print("\nintegration currency, read from HACS")
    check(rows()["integration_update"]["state"] == OK, "matching versions pass")

    behind = rows(hacs_update={"found": True, "update_available": True,
                               "installed": "0.6.1", "latest": "0.6.2"})
    check(behind["integration_update"]["state"] == MISSING, "an available update is reported")
    check("0.6.2" in behind["integration_update"]["detail"]
          and "0.6.1" in behind["integration_update"]["detail"],
          "and the detail names both versions")
    # An out-of-date integration still works. Blocking is for things that stop
    # live view, and this does not.
    check(behind["integration_update"]["required"] is False, "it never blocks")

    # A hand-copied install has no HACS update entity. That is unknown, not
    # out of date - the integration cannot see any version but its own.
    manual = rows(hacs_update={"found": False})
    check(manual["integration_update"]["state"] == UNKNOWN, "no HACS entity is unknown")
    check("0.6.2" in manual["integration_update"]["detail"],
          "and it still names the version that is installed")
    check(summarize(build(facts(hacs_update={"found": False})))["blocking"] == 0,
          "unknown never blocks")


def test_dashboard_resource() -> None:
    print("\nthe Lovelace dialog resource")
    check(rows()["dashboard_resource"]["state"] == OK, "a registered resource passes")
    # HACS appends a cache-buster; the registration check in __init__.py already
    # allows for it, and disagreeing here would report a resource it just added.
    check(rows(resource_urls=[f"{RESOURCE_URL}?hacstag=123"])["dashboard_resource"]["state"] == OK,
          "a cache-busting query string is still the same resource")
    check(rows(resource_urls=["/local/other.js"])["dashboard_resource"]["state"] == MISSING,
          "an unrelated resource is not mistaken for it")
    # The pre-0.6.2 path is still served, so it is registered and working.
    # Reporting it as missing would send people to fix a dashboard that works.
    legacy = rows(resource_urls=[LEGACY_RESOURCE_URL])
    check(legacy["dashboard_resource"]["state"] == OK, "the pre-0.6.2 path still counts")
    check("0.6.2" in legacy["dashboard_resource"]["detail"],
          "and the detail says which path it is on")
    check(rows(resource_urls=None)["dashboard_resource"]["state"] == UNKNOWN,
          "an unreadable resource list is unknown, not missing")

    yaml_mode = rows(resource_urls=["/local/other.js"], lovelace_mode="yaml")
    check("configuration.yaml" in yaml_mode["dashboard_resource"]["detail"],
          "YAML mode is told where to put it, not to click a button it lacks")


def test_frontend_cards() -> None:
    print("\nHACS frontend cards")
    check(rows()["button_card"]["state"] == OK, "button-card is found by its path")
    check(rows()["auto_entities"]["state"] == OK, "auto-entities is found by its path")
    bare = rows(resource_urls=[RESOURCE_URL])
    check(bare["button_card"]["state"] == MISSING, "a missing card is reported")
    check(bare["button_card"]["required"] is False,
          "neither card blocks live view, so neither is required")
    check("self-populating" in bare["auto_entities"]["detail"],
          "auto-entities says it is only for the self-populating dashboard")
    check(rows(resource_urls=None)["button_card"]["state"] == UNKNOWN,
          "no resource list means unknown for the cards too")


def test_blocking_counts_only_required_failures() -> None:
    print("\nthe summary's blocking count")
    summary = summarize(build(facts(environment={"blinkpy": "0.25.9", "ffmpeg": None},
                                    resource_urls=[RESOURCE_URL])))
    check(summary["blocking"] == 1, f"only the required failure blocks ({summary})")
    check(summary["missing"] == 3, f"the optional ones are still counted ({summary})")


def main() -> int:
    for test in (
        test_shape,
        test_all_green,
        test_home_assistant,
        test_integration_update,
        test_blink_integration,
        test_blinkpy,
        test_ffmpeg,
        test_old_proxy_is_not_a_failure,
        test_dashboard_resource,
        test_frontend_cards,
        test_blocking_counts_only_required_failures,
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
