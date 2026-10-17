"""The dialog finds a camera's entities by what they say they are, not by name.

Home Assistant chooses entity ids, and from 2026.9 an id it derives has the
device's name in front: camera.blink_live_driveway is
camera.blink_driveway_blink_live_driveway on a fresh install. The dialog used to
guess the first, find nothing, and so have no token. The live view reported the
token as not ready and Refresh Snapshot was refused without a word, on a
dashboard the panel had generated, with the entity named correctly everywhere
except the one place that mattered.

The functions that decide this have no DOM in them, so they are cut out of the
dialog and run under node against a made-up `hass`. Nothing here needs a
browser. Run from the repo root:

    python tests/test_dialog_entities.py
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIALOG = ROOT / "custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js"

CHECKS = 0
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def resolvers() -> str:
    """The dialog's entity-finding functions, as one block of source."""
    source = DIALOG.read_text()
    start = source.index("  function hasToken(state)")
    end = source.index("  function openLiveDialog(")
    return source[start:end]


HARNESS = """
const camera = (attrs) => ({ state: "idle", attributes: attrs });
const tokened = (extra) => camera({ access_token: "tok", ...extra });
const results = {};

// A fresh install on 2026.9 or later: ids carry the device's name.
const prefixed = { states: {
  "camera.blink_driveway_blink_live_driveway": tokened({ proxy_slug: "driveway", friendly_name: "Blink Live Driveway" }),
  "camera.blink_back_door_blink_live_back_door": tokened({ proxy_slug: "back_door" }),
  "camera.blink_proxy_driveway": tokened({ snapshot_of: "driveway" }),
  "camera.blink_proxy_back_door": tokened({ snapshot_of: "back_door" }),
} };
// An older install: the ids the documentation spells.
const plain = { states: {
  "camera.blink_live_driveway": tokened({ proxy_slug: "driveway" }),
  "camera.blink_proxy_driveway": tokened({ snapshot_of: "driveway" }),
} };

const idOf = (hass, state) => Object.keys(hass.states).find((id) => hass.states[id] === state) || null;

results.prefixedBySlug = idOf(prefixed, liveState(prefixed, "driveway", undefined));
results.prefixedOtherCamera = idOf(prefixed, liveState(prefixed, "back_door", ""));
results.prefixedGuessedWrong = idOf(prefixed, liveState(prefixed, "driveway", "camera.blink_live_driveway"));
results.plainBySlug = idOf(plain, liveState(plain, "driveway", undefined));
results.plainNamed = idOf(plain, liveState(plain, "driveway", "camera.blink_live_driveway"));
results.namedFirst = idOf(prefixed, liveState(prefixed, "driveway", "camera.blink_driveway_blink_live_driveway"));
results.unknownSlug = liveState(prefixed, "garage", undefined);
results.noSlugNoName = liveState(prefixed, "", undefined);
results.noHass = liveState(null, "driveway", undefined);

// A named entity that is not a live camera (no token) is not taken for one.
const mixed = { states: {
  "camera.front_porch_other": camera({}),
  "camera.blink_live_driveway": tokened({ proxy_slug: "driveway" }),
} };
results.namedWithoutToken = idOf(mixed, liveState(mixed, "driveway", "camera.front_porch_other"));

// The last resort, the documented id, when nothing carries proxy_slug yet.
const guess = { states: { "camera.blink_live_driveway": tokened({}) } };
results.guess = idOf(guess, liveState(guess, "driveway", undefined));

results.snapshotBySnapshotOf = snapshotEntityId(prefixed, "back_door", "");
results.snapshotNamed = snapshotEntityId(prefixed, "driveway", "camera.renamed_snapshot");
results.snapshotNamedExists = snapshotEntityId(prefixed, "driveway", "camera.blink_proxy_driveway");
results.snapshotGuess = snapshotEntityId({ states: {} }, "driveway", "");
results.snapshotNothing = snapshotEntityId({ states: {} }, "", "");
results.snapshotNoHass = snapshotEntityId(null, "driveway", "");

console.log(JSON.stringify(results));
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("  SKIP  node is not installed")
        return 0
    script = resolvers() + HARNESS
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(result.stderr[:2000])
        check(False, "the dialog's resolvers run under node")
        return 1
    got = json.loads(result.stdout)

    print("the live camera")
    check(
        got["prefixedBySlug"] == "camera.blink_driveway_blink_live_driveway",
        "is found by proxy_slug when the card names no entity, on a prefixed install",
    )
    check(
        got["prefixedOtherCamera"] == "camera.blink_back_door_blink_live_back_door",
        "and a second camera is not mistaken for the first",
    )
    check(
        got["prefixedGuessedWrong"] == "camera.blink_driveway_blink_live_driveway",
        "a card that names an entity that does not exist still gets its camera",
    )
    check(got["plainBySlug"] == "camera.blink_live_driveway", "is found by slug on an install with the documented ids")
    check(got["plainNamed"] == "camera.blink_live_driveway", "and by name, where the name is right")
    check(
        got["namedFirst"] == "camera.blink_driveway_blink_live_driveway",
        "a name that exists is used first",
    )
    check(got["namedWithoutToken"] == "camera.blink_live_driveway", "a named entity that cannot open a live view is passed over")
    check(got["guess"] == "camera.blink_live_driveway", "the documented id is the last resort")
    check(got["unknownSlug"] is None, "a slug nothing serves finds nothing")
    check(got["noSlugNoName"] is None and got["noHass"] is None, "and no slug, or no hass, finds nothing")

    print("the snapshot camera")
    check(got["snapshotBySnapshotOf"] == "camera.blink_proxy_back_door", "is found by snapshot_of")
    check(got["snapshotNamed"] == "camera.blink_proxy_driveway", "a name that does not exist gives way to the camera that says it is the snapshot")
    check(got["snapshotNamedExists"] == "camera.blink_proxy_driveway", "a name that exists is used")
    check(got["snapshotGuess"] == "camera.blink_proxy_driveway", "the last resort is the id the integration sets")
    check(got["snapshotNothing"] == "" and got["snapshotNoHass"] == "camera.blink_proxy_driveway", "and nothing is made up from nothing")

    print("wiring")
    source = DIALOG.read_text()
    check(
        "liveState(hass, config.slug, config.entity_id)" in source
        and "liveState(hass, slug, config.entity_id)" in source,
        "the live view and Refresh Snapshot both go through it",
    )
    check(
        not re.search(r"hass\.states\[`camera\.", source) and "`camera.${config.slug}`" not in source,
        "no handler indexes hass.states with a guessed id any more",
    )
    check("camera.${slug}" not in source, "and the stale official-integration default is gone")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for failure in FAILURES:
            print(f"  {failure}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
