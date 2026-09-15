"""Phone layout checks for the live-view player and its Camera Controls sheet.

The player page is one big f-string inside views.py, so nothing else in the
suite ever parses the JavaScript it ships or reads the rules that decide where
the sheet sits. Both matter on a phone, and both have broken before: a sheet
that compressed its cards on the notch side, and a header that painted over the
dialog's own close button.

These render the template with placeholder values and assert against the result
rather than against the source, so a change to how the page is assembled cannot
quietly pass.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEWS = ROOT / "custom_components/blink_liveview_proxy/views.py"

CHECKS = 0
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def player_page() -> str:
    """Render the player f-string with placeholder values."""
    source = VIEWS.read_text()
    body = source.split('    return f"""', 1)[1].split('"""', 1)[0]
    names = set(re.findall(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})", body))
    return body.format(**dict.fromkeys(names, "0"))


def player_script(page: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
    return "\n".join(blocks)


def test_javascript_parses() -> None:
    print("player javascript")
    script = player_script(player_page())
    check(len(script) > 10000, "the player ships a script block")
    node = shutil.which("node")
    if not node:
        print("  SKIP  node is not installed, cannot parse-check")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    check(result.returncode == 0, "the player javascript parses")
    if result.returncode:
        print(result.stderr[:2000])


def test_portrait() -> None:
    print("portrait")
    page = player_page()
    check(".sheet.bottom" in page, "portrait gets the bottom sheet")
    check("@media (max-width: 720px)" in page, "the phone media query exists")
    check("translateY(calc(-1 * var(--lift)))" in page,
          "the picture glides up by --lift rather than being cropped")
    check("controlsHint.parentNode !== stage) stage.appendChild(controlsHint)" in page,
          "the Controls pill sits under the picture in portrait")


def test_landscape() -> None:
    print("landscape")
    page = player_page()
    check(".sheet.side" in page, "landscape gets the side sheet")
    width = re.search(r"\.sheet\.side \{.*?width:([^;]+);", page, re.S)
    check(width is not None and "var(--safe-right)" in width.group(1),
          "the side sheet widens by the notch inset so its cards keep their width")
    check("controlsHint.parentNode !== liveActions) liveActions.appendChild(controlsHint)" in page,
          "the Controls chip joins the button row in landscape")


def test_notch() -> None:
    print("notch")
    page = player_page()
    check('setProperty("--safe-left", angle === 90' in page
          and 'setProperty("--safe-right", angle === -90' in page,
          "the notch side comes from the rotation angle, not the reported insets")
    stage = re.search(r"\n\.stage \{(.*?)\}\}?", page, re.S)
    check(stage is not None and "safe-area-inset" not in stage.group(1),
          "the picture still paints behind the notch, the frame is never inset")


def test_saving_state() -> None:
    print("saving state")
    page = player_page()
    check("el.disabled = true" in page and 'setAttribute("aria-busy", "true")' in page,
          "a control is really disabled while its change is in flight")
    check("The camera was busy" in page and "The camera refused" in page,
          "a busy camera and a refusal read differently")
    check("Volume applies to the next live view" in page,
          "the volume slider says when it takes effect")
    load = page.split("async function loadControls", 1)[1].split("\nconst ", 1)[0]
    check("const startedAt = controlsEpoch;" in load and "if (startedAt !== controlsEpoch) return;" in load,
          "a read that started before a write cannot land after it")
    check("controlsEpoch += 1;" in page.split("async function sendControls", 1)[1][:400],
          "and every write marks reads already in flight as stale")

    send = page.split("async function sendControls", 1)[1].split("\nfunction ", 1)[0]
    check("finally" in send and "renderControls();" in send.split("finally", 1)[1],
          "the sheet re-reads its state after every write, success or failure")
    check("locked.forEach((el) => { el.disabled = false; });" in send.split("finally", 1)[1],
          "and hands the control back even when the write threw")


def main() -> int:
    test_javascript_parses()
    test_portrait()
    test_landscape()
    test_notch()
    test_saving_state()
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
