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

import ast
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
    # Python processes escapes in the f-string before the browser ever sees
    # it: a "\\n" in the source reaches the page as "\n", but a bare "\n" reaches
    # it as a line break, which ends a JavaScript string literal mid-line.
    # Evaluating the literal first is what makes the parse check below see
    # the same script the browser gets.
    body = ast.literal_eval('"""' + body + '"""')
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
    check(
        "translateY(calc(-1 * var(--lift)))" in page,
        "the picture glides up by --lift rather than being cropped",
    )
    check(
        "controlsHint.parentNode !== stage) stage.appendChild(controlsHint)" in page,
        "the Controls pill sits under the picture in portrait",
    )


def test_landscape() -> None:
    print("landscape")
    page = player_page()
    check(".sheet.side" in page, "landscape gets the side sheet")
    width = re.search(r"\.sheet\.side \{.*?width:([^;]+);", page, re.S)
    check(
        width is not None and "var(--safe-right)" in width.group(1),
        "the side sheet widens by the notch inset so its cards keep their width",
    )
    check(
        "controlsHint.parentNode !== liveActions) liveActions.appendChild(controlsHint)" in page,
        "the Controls chip joins the button row in landscape",
    )


def test_notch() -> None:
    print("notch")
    page = player_page()
    check(
        'setProperty("--safe-left", angle === 90' in page
        and 'setProperty("--safe-right", angle === -90' in page,
        "the notch side comes from the rotation angle, not the reported insets",
    )
    stage = re.search(r"\n\.stage \{(.*?)\}\}?", page, re.S)
    check(
        stage is not None and "safe-area-inset" not in stage.group(1),
        "the picture still paints behind the notch, the frame is never inset",
    )


def test_saving_state() -> None:
    print("saving state")
    page = player_page()
    check(
        "el.disabled = true" in page and 'setAttribute("aria-busy", "true")' in page,
        "a control is really disabled while its change is in flight",
    )
    check(
        "The camera was busy" in page and "The camera refused" in page,
        "a busy camera and a refusal read differently",
    )
    check(
        "Applies to the next live view, not this one." in page,
        "the volume slider says when it takes effect",
    )
    check(
        "/controls?token=" in page and "&session=${encodeURIComponent(sessionId)}" in page,
        "the controls route is told which live view is this player's, so the lamp can ride it",
    )
    check(
        "Not confirmed by the camera yet." in page,
        "a lamp the camera has not yet reported reads as unconfirmed, not as next-session",
    )
    check(
        "Will be set when this live view ends." in page and "after your last live view" in page,
        "a setting held back mid-stream says when it lands; the next open says what happened",
    )
    check(
        page.index('id="sheetNote"') < page.index('id="cardFlood"'),
        "the sheet's own line sits at the top, where it is seen however long the sheet",
    )
    bright = page.split('id="cardBright"', 1)[1].split('id="cardTemp"', 1)[0]
    volume = page.split('id="cardVolume"', 1)[1].split('id="lightSection"', 1)[0]
    check(
        'class="hint"' in bright and 'class="hint"' in volume,
        "the slider cards have a line of their own for the message",
    )
    talk = page.split("function talkMessage", 1)[1].split("\nfunction ", 1)[0]
    check(
        "liveNotice.textContent = message;" in talk and "liveNotice.hidden = !message;" in talk,
        "the notice at the top of the picture is the microphone's, exactly as before",
    )
    check(
        "setDeferredNotice" not in page and "Will be set when this live view ends" not in talk,
        "the held-back line stays in the sheet and never reaches the notice",
    )
    load = page.split("async function loadControls", 1)[1].split("\nconst ", 1)[0]
    check(
        "const startedAt = controlsEpoch;" in load
        and "if (startedAt !== controlsEpoch) return;" in load,
        "a read that started before a write cannot land after it",
    )
    check(
        "controlsEpoch += 1;" in page.split("async function sendControls", 1)[1][:400],
        "and every write marks reads already in flight as stale",
    )

    send = page.split("async function sendControls", 1)[1].split("\nfunction ", 1)[0]
    check(
        "finally" in send and "renderControls();" in send.split("finally", 1)[1],
        "the sheet re-reads its state after every write, success or failure",
    )
    check(
        "locked.forEach((el) => { el.disabled = false; });" in send.split("finally", 1)[1],
        "and hands the control back even when the write threw",
    )
    check(
        "cardLines.set(card, " in send and "sheetNote" not in send,
        "a write's message goes to the card that was touched, not to a line elsewhere",
    )


def test_start_watchdog() -> None:
    """A live view that never puts a picture up must say so, and say which kind.

    Issue #80: a camera announced audio it never sent, mpegts.js buffered the
    video and never showed it, and the dialog sat on "Waking camera and waiting
    for video" with no end. The cause was fixed in the proxy, but the page was
    also at fault: play() does not settle until media is playing, so on a stream
    that never starts nothing written after `await startPlayback()` ever ran -
    not the session-limit timer either. A watchdog armed after that await would
    be dead code, and this is the check that says so.
    """
    print("start watchdog")
    page = player_page()
    check("const START_TIMEOUT_MS = 30000" in page, "a picture has thirty seconds to appear")
    arms = [m.start() for m in re.finditer(r"armStartWatchdog\(\);", page)]
    awaits = [m.start() for m in re.finditer(r"await startPlayback\(\);", page)]
    check(len(awaits) == 2, "both the MSE and the HLS start paths wait for playback")
    check(
        all(any(0 < wait - arm < 400 for arm in arms) for wait in awaits),
        "the watchdog is armed before each await, not after it",
    )
    check(page.count("disarmStartWatchdog();\n      video.classList.add(\"ready\")") == 1
          and page.count("disarmStartWatchdog();\n    video.classList.add(\"ready\")") == 1,
          "a picture appearing disarms it on both paths")
    check("  disarmStartWatchdog();\n  if (endTimer)" in page, "ending a session disarms it")
    check(
        page.count("if (tapNeeded) disarmStartWatchdog();") == 2 and page.count("tapNeeded = true;") == 2,
        "a browser that wants a tap to start is waiting for the viewer, not stalled",
    )
    check("bytesArrived = true" in page and "mpegts.Events.STATISTICS_INFO" in page,
          "it learns from the player whether any media arrived")
    check("The camera did not start sending video" in page and "Video is arriving, but" in page,
          "and says which of the two it was")
    check(page.count("endSession(sessionLimitMessage())") == 2,
          "the session limit no longer claims a live view finished that never began")
    check(
        len(re.findall(r"second live view finished", page)) == 1,
        "that sentence is said in one place only, and only for a view that played",
    )


def test_failure_codes() -> None:
    """Every failure the player can end on carries a code, and each is documented.

    Issue #91: an iPhone that never requested a segment was told the camera had
    not started sending video, while the camera was streaming. The page had one
    message for "nothing arrived" on both players, and only a screenshot showed
    which branch had fired. Codes make that one line of text in an issue.
    """
    print("failure codes")
    page = player_page()
    table = page.split("const FAILURES = {", 1)[1].split("};", 1)[0]
    defined = re.findall(r'"(E-\d{3}[a-z]?)":', table)
    # Raised anywhere outside the table: in a failureMessage() call, or as one
    # side of the ternary that picks between two.
    rest = page.replace(table, "")
    used = set(re.findall(r'"(E-\d{3}[a-z]?)"', rest))
    check(len(defined) == len(set(defined)), "no code is defined twice")
    check(used <= set(defined), f"every code raised is defined: {sorted(used - set(defined))}")
    check(set(defined) <= used, f"every code defined is raised: {sorted(set(defined) - used)}")

    docs = (ROOT / "docs/OPERATIONS.md").read_text()
    dialog = (ROOT / "custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js").read_text()
    dialog_codes = set(re.findall(r"\((E-\d{3}[a-z]?)\)", dialog))
    check(dialog_codes == {"E-010"}, "the dialog raises its own code")
    check(not dialog_codes & set(defined), "and it does not reuse one of the player's")
    undocumented = sorted(code for code in set(defined) | dialog_codes if f"`{code}`" not in docs)
    check(not undocumented, f"every code is in docs/OPERATIONS.md: {undocumented}")

    # The endings that are not failures stay free of codes.
    check('endSession("Live view ended.")' in page, "a stream that ended is not an error")
    check("Live view ended or the camera stopped sending video.\")" not in page,
          "no failure is ended with bare text any more")
    check('setEnded("Could not save' not in page, "including a failed save")

    hls_nothing = re.search(r'"E-005": "([^"]+)"', table).group(1)
    check("camera did not start" not in hls_nothing.lower(),
          "the HLS no-video message does not blame the camera (#91)")
    check('transport === "hls"' in page and len(re.findall(r'^\s*transport = "(hls|mse)";', page, re.M)) == 2,
          "the page knows which player it ran, to pick the code")



def test_diagnostics() -> None:
    """A failed live view offers a report, and copying it works on plain HTTP."""
    print("diagnostics")
    page = player_page()
    check('id="diagnostics"' in page and 'id="diagText"' in page,
          "the failure screen has a Copy diagnostics button and a fallback text box")
    check("if (lastFailure && !diagReport) prepareDiagnostics();" in page,
          "only a failure offers it, and the report is fetched as soon as it fails")
    copy = page.split("function copyDiagnostics()", 1)[1].split("\nfunction ", 1)[0]
    check("await" not in copy, "copying happens inside the tap, with no await before it (iOS)")
    check('document.execCommand("copy")' in copy and "readOnly = false" in copy,
          "it copies with execCommand, which needs no HTTPS, from an editable field (iOS)")
    check("stopDiagnostics();\n  disarmStartWatchdog();" in page,
          "tearing the player down is not recorded as part of the failure")
    check("&session=${session}" in page.split("function stopUrl()", 1)[1][:300],
          "the stop request joins the session's trace")

    # Unsent reports: saved on the device by the player, sent later by the
    # player or by the dashboard script (which the panel loads too). The panel
    # itself never touches browser storage; test_authentication holds it to that.
    panel = (ROOT / "custom_components/blink_liveview_proxy/frontend/blink-proxy-auth-panel.js").read_text()
    dialog = (ROOT / "custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js").read_text()
    key = re.search(r'const PENDING_KEY = "([^"]+)"', page)
    check(bool(key) and f'const PENDING_DIAGNOSTICS_KEY = "{key.group(1)}"' in dialog,
          "the player and the dashboard script use the same key for unsent reports")
    check("if (!report && queueDiagnostics(payload))" in page,
          "a report that could not be sent is kept on the device")
    check("setTimeout(flushPending, " in page and "sendPendingDiagnostics(hass)" in dialog,
          "and sent later, by this camera's player or by any Home Assistant page")
    event = re.search(r'const DIAGNOSTICS_SENT_EVENT = "([^"]+)"', dialog)
    check(bool(event) and f'const DIAGNOSTICS_SENT_EVENT = "{event.group(1)}"' in panel,
          "the panel hears when they have been sent, and reloads its list")
    check("started_at: diag ? diag.wall" in page,
          "a late report carries when it failed, not when it was sent")
    copy_text = panel.split("async _copyText(text)", 1)[1].split("\n  }\n", 1)[0]
    check(copy_text.index('execCommand("copy")') < copy_text.index("navigator.clipboard"),
          "the panel copies with execCommand first, so plain HTTP works there too")
    check('data-diag="${index}"' in panel and "_diagnosticsHtml(camera)" in panel,
          "each camera card in the panel has its Diagnostics list")


def main() -> int:
    test_javascript_parses()
    test_portrait()
    test_landscape()
    test_notch()
    test_saving_state()
    test_start_watchdog()
    test_failure_codes()
    test_diagnostics()
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
