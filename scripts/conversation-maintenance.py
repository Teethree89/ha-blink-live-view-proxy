#!/usr/bin/env python3
"""Coordinate /compact and /clear requests at safe Codex checkpoints.

Agents cannot execute chat slash commands from a shell. This helper records
maintenance state, then drains safe app-server maintenance from either an
explicit finished-task command or a Codex Stop hook.

/compact is queued during active work and drained after the assistant turn
stops. /clear is a hard task-boundary action: queue it when the user changes
tasks, asks for a hard pivot, or starts a clearly unrelated thread of work.

Nothing external can create or run a thread inside the VS Code Codex sidebar, so
the /clear handoff is delivered in-process by Codex hooks instead. On /clear the
handoff prompt is written verbatim to a durable sidecar feed file
(.runtime/handoff-prompt.md) and copied to the clipboard. The project's
SessionStart hook then runs `handoff --emit-context`, which injects that feed as
`additionalContext` into the next new thread the user opens and consumes it so it
fires once. Opening the new thread stays a user action (the native New Thread
button); the hook makes it a primed, no-paste handoff.

Because the clipboard is easily clobbered, the `handoff` subcommand can also
re-copy, show, locate, or clear the feed on demand.

Examples:
  python3 scripts/conversation-maintenance.py start "hvac dashboard work"
  python3 scripts/conversation-maintenance.py compact --reason "Context is long"
  python3 scripts/conversation-maintenance.py drain-hook
  python3 scripts/conversation-maintenance.py finish
  python3 scripts/conversation-maintenance.py compact-now --force
  python3 scripts/conversation-maintenance.py refresh-ui --refresh-mode window
  python3 scripts/conversation-maintenance.py clear --reason "New task"
  python3 scripts/conversation-maintenance.py handoff --copy
  python3 scripts/conversation-maintenance.py status
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / ".runtime" / "conversation-maintenance.json"
DEFAULT_LOCK = ROOT / ".runtime" / "conversation-maintenance.lock"
# Sidecar handoff feed: the latest /clear handoff prompt, persisted verbatim so
# it survives clipboard churn and can be re-copied or read on demand (the
# `handoff` subcommand) or consumed directly by the next Codex thread.
DEFAULT_FEED = ROOT / ".runtime" / "handoff-prompt.md"
# When a focus-grabbing keystroke path steals focus from a non-VS-Code app, the
# app it interrupted is recorded here so the final step (the kickoff) can hand
# focus back. Only written when VS Code was NOT already frontmost.
DEFAULT_PREV_APP = ROOT / ".runtime" / "prev-frontmost-app.txt"
LOCK_STALE_SECONDS = 15 * 60
# The `codex` CLI is normally on PATH; CODEX_BIN allows pointing at the binary
# bundled with the VS Code Codex extension (or any explicit install) for testing.
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
VSCODE_BUNDLE_ID = os.environ.get("VSCODE_BUNDLE_ID", "com.microsoft.VSCode")
VSCODE_PROCESS_NAME = os.environ.get("VSCODE_PROCESS_NAME", "Code")
WEBVIEW_REFRESH_COMMAND = "workbench.action.webview.reloadWebviewAction"
WEBVIEW_REFRESH_QUERY = "Developer: Reload Webviews"
WINDOW_RELOAD_COMMAND = "workbench.action.reloadWindow"
WINDOW_RELOAD_QUERY = "Developer: Reload Window"
CLIENT_INFO = {
    "name": "conversation-maintenance",
    "title": "Conversation Maintenance",
    "version": "0.3.0",
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_state() -> dict[str, Any]:
    return {
        "active_task": None,
        "queue": [],
        "last_delivery": None,
        "pending_clear": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not read state file {path}: {exc}") from exc

    base = empty_state()
    base.update(state)
    if not isinstance(base["queue"], list):
        base["queue"] = []
    return base


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                path.unlink()
                return acquire_lock(path)
        except FileNotFoundError:
            return acquire_lock(path)
        print(f"Another conversation-maintenance drain is already running: {path}")
        return None

    payload = f"pid={os.getpid()} started_at={now()}\n"
    os.write(fd, payload.encode("utf-8"))
    return fd


def release_lock(path: Path, fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def find_thread_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("threadId", "thread_id", "conversationId", "conversation_id"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        for nested in value.values():
            found = find_thread_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_thread_id(nested)
            if found:
                return found
    return None


def thread_id_from_stdin() -> str | None:
    try:
        if sys.stdin.isatty():
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
    except (OSError, ValueError):
        return None
    if not ready:
        return None

    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return find_thread_id(payload)


def compact_via_app_server(
    thread_id: str | None,
    transport: str = "auto",
    sock: Path | None = None,
    timeout: float = 120.0,
) -> tuple[bool, str]:
    if not thread_id:
        return (
            False,
            "Cannot run compact: no thread id was provided and CODEX_THREAD_ID is not set.",
        )

    if transport == "auto":
        ok, message = compact_via_app_server(thread_id, "proxy", sock, timeout)
        if ok:
            return ok, message
        if "no live Codex app-server control socket" not in message:
            return ok, message
        fallback_ok, fallback_message = compact_via_app_server(thread_id, "stdio", None, timeout)
        if fallback_ok:
            return fallback_ok, fallback_message + " Used stdio fallback."
        return fallback_ok, fallback_message

    if transport == "proxy":
        cmd = [CODEX_BIN, "app-server", "proxy"]
        if sock:
            cmd.extend(["--sock", str(sock)])
    elif transport == "stdio":
        cmd = [CODEX_BIN, "app-server", "--stdio"]
    else:
        return False, f"Unknown app-server transport: {transport}"

    try:
        ok, message = run_compact_rpc(cmd, thread_id, timeout)
        return ok, message
    except FileNotFoundError:
        return False, f"Cannot run compact: `{CODEX_BIN}` was not found on PATH."


def run_compact_rpc(cmd: list[str], thread_id: str, timeout: float) -> tuple[bool, str]:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_lines: list[str] = []

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def read_until(
        predicate: Any, deadline: float
    ) -> dict[str, Any] | None:
        assert proc.stdout is not None
        assert proc.stderr is not None
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.25)
            for stream in ready:
                line = stream.readline()
                if not line:
                    continue
                if stream is proc.stderr:
                    stderr_lines.append(line.rstrip())
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if predicate(message):
                    return message
            if proc.poll() is not None:
                leftover = proc.stderr.read()
                if leftover:
                    stderr_lines.extend(line for line in leftover.splitlines() if line)
                return None
        return None

    def read_until_id(wanted_id: int, deadline: float) -> dict[str, Any] | None:
        return read_until(lambda message: message.get("id") == wanted_id, deadline)

    def is_compaction_complete(message: dict[str, Any]) -> bool:
        if message.get("method") == "thread/compacted":
            return True
        if message.get("method") != "item/completed":
            return False
        item = message.get("params", {}).get("item", {})
        return item.get("type") == "contextCompaction"

    deadline = time.time() + timeout
    try:
        send(
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": CLIENT_INFO,
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        initialized = read_until_id(0, deadline)
        if initialized is None:
            return compact_rpc_timeout(proc, timeout, stderr_lines)
        if initialized.get("error"):
            return False, f"App-server initialize was rejected: {initialized['error']}"

        send({"method": "initialized", "params": {}})
        send({"id": 1, "method": "thread/resume", "params": {"threadId": thread_id, "cwd": str(ROOT)}})
        resumed = read_until_id(1, deadline)
        if resumed is None:
            return compact_rpc_timeout(proc, timeout, stderr_lines)
        if resumed.get("error"):
            return False, f"App-server could not resume thread {thread_id}: {resumed['error']}"

        send({"id": 2, "method": "thread/compact/start", "params": {"threadId": thread_id}})
        started = read_until_id(2, deadline)
        if started is None:
            return compact_rpc_timeout(proc, timeout, stderr_lines)
        if started.get("error"):
            return False, f"Compact RPC was rejected: {started['error']}"

        compacted = read_until(is_compaction_complete, deadline)
        if compacted is None:
            return (
                False,
                "Compact RPC was accepted, but no compaction completion notification "
                f"arrived within {timeout:g}s.",
            )
        return True, f"Completed /compact for thread {thread_id}."
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def compact_rpc_timeout(
    proc: subprocess.Popen[str], timeout: float, stderr_lines: list[str]
) -> tuple[bool, str]:
    returncode = proc.poll()
    details = "\n".join(line for line in stderr_lines if line)
    if returncode not in (None, 0):
        if "failed to connect to socket" in details:
            return (
                False,
                "Cannot run compact: no live Codex app-server control socket is available.\n"
                "Use `--transport auto` or `--transport stdio` to spawn a temporary "
                "app-server fallback.",
            )
        if details:
            return False, f"Compact RPC failed with exit code {returncode}.\n{details}"
        return False, f"Compact RPC failed with exit code {returncode}."
    if details:
        return False, f"Compact RPC timed out after {timeout:g}s.\n{details}"
    return False, f"Compact RPC timed out after {timeout:g}s."


def write_handoff_feed(feed: Path, prompt: str) -> str:
    """Persist the handoff prompt verbatim to the sidecar feed file."""
    feed.parent.mkdir(parents=True, exist_ok=True)
    text = prompt if prompt.endswith("\n") else prompt + "\n"
    tmp = feed.with_suffix(feed.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, feed)
    return str(feed)


def read_handoff_feed(feed: Path) -> str | None:
    try:
        value = feed.read_text(encoding="utf-8")
    except OSError:
        return None
    return value if value.strip() else None


# ---------------------------------------------------------------------------
# OS automation layer
#
# Everything that talks to the desktop — querying the foreground window,
# activating VS Code, opening vscode:// URLs, synthesizing keystrokes, restoring
# focus, and the clipboard — funnels through the small set of primitives below,
# so the rest of the script stays OS-agnostic. macOS drives them with
# `osascript` / `open` / `pbcopy`; Windows uses Windows PowerShell (Forms
# SendKeys + Win32 SetForegroundWindow + clip). Keystroke programs are expressed
# as a tiny platform-neutral step list — ("text", s), ("return",), ("delay", f),
# ("palette",) — that each backend renders in its own dialect. Other platforms
# degrade to a clear "unsupported" message instead of crashing.
# ---------------------------------------------------------------------------

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def _run(cmd: list[str], timeout: float, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)


def _powershell(script: str, timeout: float = 10.0, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a Windows PowerShell snippet (5.1, always present as powershell.exe)."""
    return _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout=timeout,
        **kwargs,
    )


def applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# SendKeys treats these as control characters; a literal one must be wrapped in
# braces (e.g. `+` -> `{+}`). See the .NET SendKeys.SendWait documentation.
_SENDKEYS_SPECIAL = set("+^%~(){}[]")


def _sendkeys_escape(text: str) -> str:
    return "".join("{" + ch + "}" if ch in _SENDKEYS_SPECIAL else ch for ch in text)


def _ps_single_quote(text: str) -> str:
    """Escape a Python string for a PowerShell single-quoted literal."""
    return text.replace("'", "''")


# Win32 shim reused by the foreground-query and focus primitives. `@' ... '@` is
# a non-interpolating here-string, so the embedded C# double quotes are literal.
_WIN_USER32 = (
    "Add-Type @'\n"
    "using System;\n"
    "using System.Runtime.InteropServices;\n"
    "public static class CMWin {\n"
    '  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();\n'
    '  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);\n'
    '  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);\n'
    '  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);\n'
    "}\n"
    "'@\n"
)


def _mac_frontmost_app() -> tuple[bool, str]:
    try:
        result = _run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to return name of first '
                "application process whose frontmost is true",
            ],
            timeout=10,
        )
    except FileNotFoundError:
        return False, "`osascript` not found (not macOS?)."
    except subprocess.TimeoutExpired:
        return False, "Timed out querying the frontmost app."
    if result.returncode != 0:
        return False, result.stderr.strip() or "System Events query failed (Accessibility?)."
    return True, result.stdout.strip()


def _win_foreground_app() -> tuple[bool, str]:
    """Return the ProcessName (no .exe) owning the Win32 foreground window."""
    script = _WIN_USER32 + (
        "$h=[CMWin]::GetForegroundWindow(); $procId=0;\n"
        "[void][CMWin]::GetWindowThreadProcessId($h,[ref]$procId);\n"
        "$p=Get-Process -Id $procId -ErrorAction SilentlyContinue;\n"
        "if($p){ Write-Output $p.ProcessName } else { exit 1 }\n"
    )
    try:
        result = _powershell(script)
    except FileNotFoundError:
        return False, "`powershell` not found (not Windows?)."
    except subprocess.TimeoutExpired:
        return False, "Timed out querying the foreground window."
    if result.returncode != 0:
        return False, result.stderr.strip() or "Foreground-window query failed."
    return True, result.stdout.strip()


def _win_focus_process(name: str) -> tuple[bool, str]:
    """Raise the main window of the process named `name` (no .exe) to the front."""
    safe = _ps_single_quote(name)
    script = _WIN_USER32 + (
        f"$p=Get-Process -Name '{safe}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1;\n"
        "if($p){ [void][CMWin]::ShowWindow($p.MainWindowHandle,9); "
        "[void][CMWin]::SetForegroundWindow($p.MainWindowHandle); Write-Output 'ok' } "
        "else { exit 1 }\n"
    )
    try:
        result = _powershell(script)
    except FileNotFoundError:
        return False, "`powershell` not found (not Windows?)."
    except subprocess.TimeoutExpired:
        return False, f"Timed out focusing process {name!r}."
    if result.returncode != 0:
        return False, result.stderr.strip() or f"No focusable window for process {name!r}."
    return True, f"Focused process {name!r}."


def frontmost_app() -> tuple[bool, str]:
    """Return (ok, name) of the frontmost app; name is the OS process name."""
    if IS_MACOS:
        return _mac_frontmost_app()
    if IS_WINDOWS:
        return _win_foreground_app()
    return False, f"Foreground-app query is unsupported on platform {sys.platform!r}."


def open_vscode_url(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Open a vscode:// URL, which also brings VS Code to the foreground."""
    if IS_MACOS:
        try:
            completed = _run(["open", "-b", VSCODE_BUNDLE_ID, url], timeout=timeout)
        except FileNotFoundError:
            return False, "macOS `open` was not found."
        except subprocess.TimeoutExpired:
            return False, f"Timed out opening {url}."
        if completed.returncode != 0:
            return False, completed.stderr.strip() or f"Failed to open {url}."
        return True, f"Opened {url}."
    if IS_WINDOWS:
        script = f"Start-Process '{_ps_single_quote(url)}'"
        try:
            completed = _powershell(script, timeout=timeout)
        except FileNotFoundError:
            return False, "`powershell` was not found."
        except subprocess.TimeoutExpired:
            return False, f"Timed out opening {url}."
        if completed.returncode != 0:
            return False, completed.stderr.strip() or f"Failed to open {url}."
        return True, f"Opened {url}."
    return False, f"Opening a vscode:// URL is unsupported on platform {sys.platform!r}."


def _restore_focus_to(name: str) -> tuple[bool, str]:
    """Raise the still-running app/process `name` back to the foreground."""
    if IS_MACOS:
        escaped = applescript_escape(name)
        try:
            result = _run(
                [
                    "osascript",
                    "-e",
                    f'tell application "System Events" to set frontmost of process "{escaped}" to true',
                ],
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return False, f"Could not restore focus to {name!r}: {exc}"
        if result.returncode != 0:
            return False, result.stderr.strip() or f"Failed to restore focus to {name!r}."
        return True, f"Restored focus to {name!r}."
    if IS_WINDOWS:
        ok, _msg = _win_focus_process(name)
        if not ok:
            return False, f"Failed to restore focus to {name!r}."
        return True, f"Restored focus to {name!r}."
    return False, f"Focus restore is unsupported on platform {sys.platform!r}."


def _mac_send_keys(program: list[tuple], timeout: float) -> tuple[bool, str]:
    body = ['tell application "System Events"']
    for step in program:
        kind = step[0]
        if kind == "text":
            body.append(f'\tkeystroke "{applescript_escape(step[1])}"')
        elif kind == "return":
            body.append("\tkeystroke return")
        elif kind == "palette":
            body.append('\tkeystroke "p" using {command down, shift down}')
        elif kind == "delay":
            body.append(f"\tdelay {max(0.0, step[1]):g}")
    body.append("end tell")
    script = "\n".join(body)
    try:
        result = _run(["osascript", "-e", script], timeout=timeout)
    except FileNotFoundError:
        return False, "`osascript` was not found."
    except subprocess.TimeoutExpired:
        return False, "Timed out sending keystrokes."
    if result.returncode != 0:
        return False, result.stderr.strip() or "Failed to send keystrokes."
    return True, ""


def _win_send_keys(program: list[tuple], timeout: float) -> tuple[bool, str]:
    lines = ["Add-Type -AssemblyName System.Windows.Forms"]
    for step in program:
        kind = step[0]
        if kind == "text":
            payload = _ps_single_quote(_sendkeys_escape(step[1]))
            lines.append(f"[System.Windows.Forms.SendKeys]::SendWait('{payload}')")
        elif kind == "return":
            lines.append("[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')")
        elif kind == "palette":
            lines.append("[System.Windows.Forms.SendKeys]::SendWait('^+p')")
        elif kind == "delay":
            lines.append(f"Start-Sleep -Milliseconds {int(max(0.0, step[1]) * 1000)}")
    script = "\n".join(lines)
    try:
        result = _powershell(script, timeout=timeout)
    except FileNotFoundError:
        return False, "`powershell` was not found."
    except subprocess.TimeoutExpired:
        return False, "Timed out sending keystrokes."
    if result.returncode != 0:
        return False, result.stderr.strip() or "Failed to send keystrokes."
    return True, ""


def send_key_sequence(program: list[tuple], timeout: float = 10.0) -> tuple[bool, str]:
    """Render and run a platform-neutral keystroke program. Returns (ok, detail).

    `detail` is an error string on failure and empty on success. The subprocess
    timeout is padded by the program's own delay budget so a mid-sequence
    `delay`/`Start-Sleep` is never killed early.
    """
    budget = timeout + sum(
        max(0.0, step[1]) for step in program if step and step[0] == "delay"
    )
    if IS_MACOS:
        return _mac_send_keys(program, budget)
    if IS_WINDOWS:
        return _win_send_keys(program, budget)
    return False, f"Keystroke synthesis is unsupported on platform {sys.platform!r}."


def run_vscode_command_palette(
    command_id: str,
    command_query: str,
    timeout: float = 15.0,
    palette_delay: float = 0.4,
    select_delay: float = 0.2,
) -> tuple[bool, str]:
    """Run a VS Code Command Palette item via guarded keystrokes."""
    focus_ok, focus_msg = activate_vscode(settle=0.0)
    if not focus_ok:
        return False, f"Aborted VS Code UI refresh: {focus_msg}"

    ok, front = frontmost_app()
    if not ok:
        return False, f"Aborted VS Code UI refresh: {front}"
    if front != VSCODE_PROCESS_NAME:
        return False, (
            "Aborted VS Code UI refresh: VS Code is not frontmost "
            f"(front app is {front!r}); no keys were sent."
        )

    ok, detail = send_key_sequence(
        [
            ("palette",),
            ("delay", palette_delay),
            ("text", command_query),
            ("delay", select_delay),
            ("return",),
        ],
        timeout=timeout,
    )
    if not ok:
        return False, f"Failed to run VS Code command {command_id}: {detail}"
    return (
        True,
        f"Requested VS Code command {command_id} via Command Palette query {command_query!r}.",
    )


def open_codex_thread_route(thread_id: str, timeout: float = 10.0) -> tuple[bool, str]:
    ok, msg = open_vscode_url(f"vscode://openai.chatgpt/local/{thread_id}", timeout=timeout)
    if not ok:
        return False, f"Failed to restore Codex thread {thread_id}: {msg}"
    return True, f"Restored VS Code Codex route /local/{thread_id}."


def schedule_codex_thread_restore(thread_id: str, delay: float) -> tuple[bool, str]:
    """Spawn a detached child that waits `delay`, then opens the Codex thread URL."""
    url = f"vscode://openai.chatgpt/local/{thread_id}"
    delay_text = f"{max(0.0, delay):g}"
    if IS_WINDOWS:
        ms = int(max(0.0, delay) * 1000)
        ps = f"Start-Sleep -Milliseconds {ms}; Start-Process '{_ps_single_quote(url)}'"
        argv = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ]
        popen_kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)}
    elif IS_MACOS:
        argv = [
            "/bin/sh",
            "-c",
            'sleep "$1"; open -b "$2" "$3"',
            "conversation-maintenance-restore",
            delay_text,
            VSCODE_BUNDLE_ID,
            url,
        ]
        popen_kwargs = {"start_new_session": True}
    else:
        return False, f"Codex thread restore is unsupported on platform {sys.platform!r}."
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except FileNotFoundError:
        return False, "Cannot schedule Codex thread restore: launcher was not found."
    except OSError as exc:
        return False, f"Cannot schedule Codex thread restore: {exc}"
    return True, f"Scheduled VS Code Codex route restore /local/{thread_id} after {delay_text}s."


def refresh_vscode_codex_ui(
    timeout: float = 15.0,
    fallback: bool = True,
    mode: str = "window",
    restore_thread_id: str | None = None,
    restore_delay: float = 3.0,
) -> tuple[bool, str]:
    if mode == "window":
        restore_message = None
        if restore_thread_id:
            restore_ok, restore_message = schedule_codex_thread_restore(
                restore_thread_id, restore_delay
            )
            if not restore_ok:
                return False, restore_message
        ok, message = run_vscode_command_palette(
            WINDOW_RELOAD_COMMAND,
            WINDOW_RELOAD_QUERY,
            timeout=timeout,
        )
        if restore_message:
            message = f"{message} {restore_message}"
        if not ok:
            return ok, message
        return ok, message
    if mode != "webview":
        return False, f"Unknown VS Code refresh mode: {mode}"

    ok, message = run_vscode_command_palette(
        WEBVIEW_REFRESH_COMMAND,
        WEBVIEW_REFRESH_QUERY,
        timeout=timeout,
    )
    if ok or not fallback:
        return ok, message

    fallback_ok, fallback_message = refresh_vscode_codex_ui(
        timeout=timeout,
        fallback=False,
        mode="window",
        restore_thread_id=restore_thread_id,
        restore_delay=restore_delay,
    )
    if fallback_ok:
        return True, f"{message} Fallback succeeded: {fallback_message}"
    return False, f"{message} Fallback failed: {fallback_message}"


def compact_ui_refresh(
    args: argparse.Namespace, thread_id: str | None = None
) -> tuple[bool, str] | None:
    if not getattr(args, "refresh_ui", True):
        return None
    restore_thread_id = thread_id if getattr(args, "restore_thread", True) else None
    return refresh_vscode_codex_ui(
        timeout=getattr(args, "refresh_timeout", 15.0),
        fallback=getattr(args, "refresh_fallback", True),
        mode=getattr(args, "refresh_mode", "window"),
        restore_thread_id=restore_thread_id,
        restore_delay=getattr(args, "restore_delay", 3.0),
    )


def record_ui_refresh(
    state: dict[str, Any],
    refresh_result: tuple[bool, str] | None,
) -> bool:
    if refresh_result is None:
        if state.get("last_delivery"):
            state["last_delivery"]["ui_refresh"] = "skipped"
        return True
    refresh_ok, refresh_message = refresh_result
    print(refresh_message)
    if state.get("last_delivery"):
        state["last_delivery"]["ui_refresh"] = refresh_ok
        state["last_delivery"]["ui_refresh_message"] = refresh_message
    return refresh_ok


def activate_vscode(settle: float = 0.6) -> tuple[bool, str]:
    """Bring VS Code frontmost so synthesized keystrokes land in it.

    macOS activates via `open -b <bundle>` (activates without opening anything);
    Windows raises the VS Code main window via Win32 SetForegroundWindow. We then
    pause `settle` seconds for the window to actually take focus. Callers still run
    the `frontmost_app` guard afterwards, so if activation silently fails (VS Code
    not running, another Space/desktop, etc.) no keys are sent.
    """
    if IS_MACOS:
        try:
            opened = _run(["open", "-b", VSCODE_BUNDLE_ID], timeout=10)
        except FileNotFoundError:
            return False, "Cannot focus VS Code: macOS `open` was not found."
        except subprocess.TimeoutExpired:
            return False, "Timed out focusing VS Code."
        if opened.returncode != 0:
            return False, opened.stderr.strip() or "Failed to focus VS Code."
    elif IS_WINDOWS:
        ok, msg = _win_focus_process(VSCODE_PROCESS_NAME)
        if not ok:
            return False, f"Cannot focus VS Code: {msg}"
    else:
        return False, f"Focusing VS Code is unsupported on platform {sys.platform!r}."
    if settle > 0:
        time.sleep(settle)
    return True, "Activated VS Code."


def remember_prev_app(sidecar: Path = DEFAULT_PREV_APP) -> str | None:
    """Record the frontmost app so focus can be handed back later.

    Only records when VS Code was NOT already frontmost — if the user was already
    working in VS Code, there is nothing to restore and we leave focus alone. The
    name is written to a sidecar file so a *later* process (the detached kickoff)
    can read it after the new conversation starts. Returns the recorded name, or
    None when VS Code was frontmost (or the query failed).
    """
    ok, front = frontmost_app()
    if not ok or not front or front == VSCODE_PROCESS_NAME:
        # Nothing to restore (VS Code already frontmost, or the query failed).
        # Clear any stale sidecar so a later kickoff won't restore the wrong app.
        sidecar.unlink(missing_ok=True)
        return None
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(front, encoding="utf-8")
    except OSError:
        return None
    return front


def restore_prev_app(sidecar: Path = DEFAULT_PREV_APP) -> tuple[bool, str]:
    """Reactivate the app recorded by `remember_prev_app`, then clear the sidecar.

    Uses System Events to raise the still-running process by name (no relaunch).
    A no-op when the sidecar is absent (VS Code was frontmost to begin with).
    """
    try:
        name = sidecar.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return True, "No previous app to restore."
    except OSError as exc:
        return False, f"Could not read previous-app sidecar: {exc}"
    if not name:
        sidecar.unlink(missing_ok=True)
        return True, "No previous app recorded."
    try:
        return _restore_focus_to(name)
    finally:
        sidecar.unlink(missing_ok=True)


def send_codex_prompt_keys(
    text: str, focus_delay: float = 5.0, restore_focus: bool = False, restore_delay: float = 2.5
) -> tuple[bool, str]:
    """Open a fresh Codex chat and type `text` + Enter via synthetic keystrokes.

    The new-thread composer auto-focuses on open, so the keystrokes land in it and
    submit the turn — combined with the `UserPromptSubmit` hook that injects the
    staged handoff, this makes the pivot fully hands-off. Opening the new-thread
    URL already brings VS Code forward (an implicit focus-grab), and a safety guard
    still refuses to type unless VS Code is actually frontmost, so stray keys never
    leak into another app. Requires Accessibility permission on macOS (already
    granted here); on Windows the SendKeys path needs no special permission.

    With `restore_focus`, the app that was frontmost before we grabbed focus is
    remembered (only when it wasn't VS Code) and — after `text` is submitted and
    `restore_delay` elapses so the new thread can start — focus is handed back to
    it, mirroring the Claude Code `/clear` kickoff.
    """
    if restore_focus:
        remember_prev_app()
    open_ok, open_msg = open_vscode_url("vscode://openai.chatgpt/")
    if not open_ok:
        return False, f"Cannot auto-submit: {open_msg}"

    time.sleep(focus_delay)

    ok, front = frontmost_app()
    if not ok:
        return False, f"Aborted auto-submit: {front}"
    if front != VSCODE_PROCESS_NAME:
        return False, (
            f"Aborted auto-submit: VS Code is not frontmost (front app is {front!r}); "
            "no keys were sent."
        )

    ok, detail = send_key_sequence([("text", text), ("return",)])
    if not ok:
        return False, f"Failed to send keystrokes: {detail}"
    message = f"Auto-submitted {text!r} to the focused Codex composer."
    if restore_focus:
        if restore_delay > 0:
            time.sleep(restore_delay)
        _, restore_msg = restore_prev_app()
        message = f"{message} {restore_msg}"
    return True, message


def send_slash_command_keys(
    text: str = "/compact",
    focus_delay: float = 0.5,
    submit_delay: float = 1.0,
    grab_focus: bool = False,
) -> tuple[bool, str]:
    """Type a slash command (`/compact`, `/clear`, …) + Enter into the composer.

    This is the Claude Code counterpart to the Codex app-server `/compact` RPC.
    Claude Code cannot be told to compact/clear programmatically, but its `Stop`
    hook fires with the chat composer focused, so we synthesize the keystrokes in
    place — no new thread to open, unlike `send_codex_prompt_keys`. A safety
    guard refuses to type unless VS Code is actually frontmost, so stray keys
    never leak into another app. Requires Accessibility permission for the
    process that runs `osascript` (already granted here).

    Typing a slash command like `/compact` or `/clear` pops Claude Code's
    autocomplete menu, and the first Enter only accepts/dismisses that menu
    instead of submitting. So after typing we pause `submit_delay` seconds for the
    menu to settle, then press Enter twice: the first commits the highlighted
    command, the second actually sends it. The pause+double-return is what makes
    the command fire.

    When `grab_focus` is set, first activate VS Code so the keystrokes land even
    if the user was in another app; the frontmost guard below still runs as a
    final safety, so keys are never sent unless VS Code is actually frontmost.
    """
    if grab_focus:
        act_ok, act_msg = activate_vscode()
        if not act_ok:
            return False, f"Aborted {text} keystrokes: {act_msg}"
    if focus_delay > 0:
        time.sleep(focus_delay)

    ok, front = frontmost_app()
    if not ok:
        return False, f"Aborted {text} keystrokes: {front}"
    if front != VSCODE_PROCESS_NAME:
        return False, (
            f"Aborted {text} keystrokes: VS Code is not frontmost (front app is {front!r}); "
            "no keys were sent."
        )

    ok, detail = send_key_sequence(
        [
            ("text", text),
            ("delay", max(0.0, submit_delay)),
            ("return",),
            ("delay", 0.2),
            ("return",),
        ]
    )
    if not ok:
        return False, f"Failed to send {text} keystrokes: {detail}"
    return True, f"Auto-submitted {text!r} to the focused Claude Code composer."


def send_claude_kickoff_keys(
    text: str = "go",
    focus_delay: float = 3.0,
    grab_focus: bool = False,
    restore_focus: bool = False,
    restore_delay: float = 2.5,
) -> tuple[bool, str]:
    """Type `text` + Enter into the already-focused Claude Code composer.

    Self-starts a fresh thread after `/clear`: the SessionStart hook injects the
    staged handoff as `additionalContext`, and this (spawned detached, so the hook
    can return immediately and let Claude Code process that context first) waits
    `focus_delay` seconds for the reset composer to settle, then types the kickoff
    message so the new thread begins its autonomous loop with no user interaction.

    Unlike the Codex path, `/clear` resets the current pane in place, so there is
    no new-thread URL to open — we only wait and type. `text` is plain (e.g. "go"),
    so a single Enter submits (no slash-command autocomplete to clear). A frontmost
    guard refuses to type unless VS Code is frontmost, so stray keys never leak.

    As the final step of the pivot chain this also owns focus restoration: with
    `restore_focus`, after typing `text` it waits `restore_delay` for the new
    conversation to start, then hands focus back to whatever app the pivot
    interrupted (recorded by `remember_prev_app` when the /clear keystroke first
    grabbed focus). If VS Code was frontmost to begin with, nothing was recorded
    and focus is left on VS Code.
    """
    if focus_delay > 0:
        time.sleep(focus_delay)
    if grab_focus:
        act_ok, act_msg = activate_vscode()
        if not act_ok:
            return False, f"Aborted kickoff keystrokes: {act_msg}"
    ok, front = frontmost_app()
    if not ok:
        return False, f"Aborted kickoff keystrokes: {front}"
    if front != VSCODE_PROCESS_NAME:
        return False, (
            f"Aborted kickoff keystrokes: VS Code is not frontmost (front app is {front!r}); "
            "no keys were sent."
        )
    ok, detail = send_key_sequence([("text", text), ("delay", 0.2), ("return",)])
    if not ok:
        return False, f"Failed to send kickoff keystrokes: {detail}"
    message = f"Auto-submitted {text!r} to the focused Claude Code composer."
    if restore_focus:
        if restore_delay > 0:
            time.sleep(restore_delay)
        _, restore_msg = restore_prev_app()
        message = f"{message} {restore_msg}"
    return True, message


def spawn_detached_kickoff(
    text: str,
    focus_delay: float,
    grab_focus: bool = False,
    restore_focus: bool = False,
) -> None:
    """Fire-and-forget the Claude Code kickoff so the calling hook returns at once.

    The SessionStart hook must print its `additionalContext` JSON and exit promptly
    so Claude Code injects the handoff and focuses the reset composer; only then can
    the kickoff keystrokes land. So we re-invoke this script's `kickoff` subcommand
    in a fully detached child (new session, streams to /dev/null) that sleeps for
    `focus_delay` before typing. Best-effort: failures never break the hook.
    """
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "kickoff",
        "--text",
        text,
        "--focus-delay",
        str(focus_delay),
    ]
    if grab_focus:
        argv.append("--grab-focus")
    if restore_focus:
        argv.append("--restore-focus")
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def deliver_clear_handoff(
    prompt: str,
    cwd: Path = ROOT,
    feed: Path = DEFAULT_FEED,
    auto_submit: str | None = None,
    focus_delay: float = 5.0,
    restore_focus: bool = False,
) -> tuple[bool, str, None]:
    """Stage the handoff for a fresh Codex thread.

    The handoff prompt is written to the sidecar feed file, which the project's
    `SessionStart`/`UserPromptSubmit` hooks read (via `handoff --emit-context`)
    to auto-inject the handoff into the next thread — no paste required.

    When `auto_submit` is set (e.g. "go"), also open a fresh chat and synthesize
    those keystrokes into the auto-focused composer so the new thread starts on
    its own; the guard in send_codex_prompt_keys refuses to type unless VS Code
    is frontmost. Otherwise copy the prompt to the clipboard and focus the Codex
    panel as a fallback, leaving the actual New Thread click to the user.

    Returns (ok, message, None) — the trailing None preserves the historical
    (ok, message, thread_id) call shape; there is no live thread to report.
    """
    feed_path = write_handoff_feed(feed, prompt)
    if auto_submit:
        ok, submit_message = send_codex_prompt_keys(
            auto_submit, focus_delay, restore_focus=restore_focus
        )
        return ok, f"Handoff staged at {feed_path}. {submit_message}", None
    fallback_ok, fallback_message = open_new_thread_url(cwd, prompt)
    message = (
        f"Handoff staged at {feed_path}. Open a New Thread in Codex — the hooks "
        f"inject it automatically. {fallback_message} "
        f"(re-copy anytime with `handoff --copy`)."
    )
    return fallback_ok, message, None


def default_handoff_prompt(
    next_task: str | None,
    reason: str | None,
    summary: str | None = None,
    criteria: str | None = None,
) -> str:
    lines = [
        "Fresh thread handoff — continue this work autonomously.",
        f"Workspace: {ROOT}",
    ]
    if next_task:
        lines.append(f"Task: {next_task}")
    if reason:
        lines.append(f"Pivot reason: {reason}")
    if summary:
        lines += ["", f"Previous conversation (summary for context): {summary}"]
    lines.append("")
    if criteria:
        lines.append(
            "Success criteria (ironclad — carried over from the user's original "
            f"request; the task is done only when ALL of these measurably pass): {criteria}"
        )
    else:
        lines.append(
            "Success criteria: none were carried over — in PLAN, restate the user's "
            "original goal from the summary above and define concrete, measurable "
            "criteria yourself before doing anything else."
        )
    lines.extend(
        [
            "",
            "Operate hands-off. Do NOT open by asking a question — take a concrete "
            "action on the very first turn. Only stop to ask if you are genuinely "
            "blocked (a missing secret/credential, a destructive or irreversible step, "
            "or requirements so ambiguous the criteria above cannot be pinned down); "
            "otherwise make the most reasonable assumption, state it, and proceed.",
            "",
            "YOU choose the approach most likely to satisfy the criteria — evaluate "
            "the options and pick the best one yourself; do not ask the user which "
            "approach to take.",
            "",
            "Run this loop until the task is measurably complete:",
            "1. PLAN — restate the goal and lock the measurable success criteria "
            "(specific tests/commands/checks that must pass); choose your approach.",
            "2. IMPLEMENT — make the smallest change that moves toward the goal.",
            "3. TEST — run the relevant tests/commands and capture the real output.",
            "4. REVIEW — compare the output against the success criteria; if a "
            "criterion fails, diagnose and iterate from step 2.",
            "5. Repeat until every success criterion measurably passes.",
            "",
            "Respect the repository's own conventions and safety rules (read its "
            "AGENTS.md/CLAUDE.md first); before any irreversible or "
            "production-affecting change, snapshot or back up first. Report only once "
            "the measured criteria pass, with the evidence (commands run and their "
            "output).",
        ]
    )
    return "\n".join(lines)


def resolve_handoff_prompt(
    next_task: str | None,
    reason: str | None,
    handoff: str | None,
    handoff_file: Path | None,
    summary: str | None = None,
    criteria: str | None = None,
) -> str:
    if handoff and handoff.strip():
        return handoff.strip()
    if handoff_file:
        try:
            value = handoff_file.expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"Could not read handoff file {handoff_file}: {exc}") from exc
        if value:
            return value
    return default_handoff_prompt(next_task, reason, summary, criteria)


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    if IS_MACOS:
        tool, argv = "pbcopy", ["pbcopy"]
    elif IS_WINDOWS:
        # clip.exe reads stdin and sets the clipboard (ASCII-safe; the copy is a
        # best-effort fallback, so console-codepage limits on exotic glyphs are ok).
        tool, argv = "clip", ["clip"]
    else:
        return False, f"Clipboard copy is unsupported on platform {sys.platform!r}."
    try:
        completed = subprocess.run(
            argv,
            input=text,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, f"Cannot copy handoff prompt: `{tool}` was not found."
    except subprocess.TimeoutExpired:
        return False, "Timed out while copying the handoff prompt to the clipboard."

    if completed.returncode != 0:
        return False, completed.stderr.strip() or "Failed to copy handoff prompt to clipboard."
    return True, "Copied the handoff prompt to the clipboard."


def open_new_thread_url(cwd: Path, prompt: str | None = None) -> tuple[bool, str]:
    # The standalone Codex desktop app owns codex://. VS Code owns vscode://
    # and routes vscode://openai.chatgpt/ to the installed Codex extension.
    messages: list[str] = []
    if prompt and prompt.strip():
        ok, message = copy_to_clipboard(prompt)
        messages.append(message)
        if not ok:
            return False, " ".join(messages)

    # The URI handler routes on the absolute path and ignores query strings, so
    # the prompt cannot ride along in the URL; it travels via the clipboard
    # above. Sub-routes like /new-thread are nested children that only match when
    # navigated relatively in-app — deep-linking to them blanks the webview — so
    # land on the root route, which the sidebar handles cleanly.
    url = "vscode://openai.chatgpt/"
    open_ok, open_msg = open_vscode_url(url)
    if not open_ok:
        messages.append(f"Failed to open VS Code Codex new-thread route: {open_msg}")
        return False, " ".join(messages)
    messages.append(f"Opened the VS Code Codex home route: {url}")
    return True, " ".join(messages)


def queue_action(
    state: dict[str, Any],
    action: str,
    reason: str | None,
    thread_id: str | None = None,
    next_task: str | None = None,
    handoff_prompt: str | None = None,
) -> str:
    queued_at = now()
    request = {
        "action": action,
        "reason": reason,
        "queued_at": queued_at,
    }
    if thread_id:
        request["thread_id"] = thread_id
    if next_task:
        request["next_task"] = next_task
    if handoff_prompt:
        request["handoff_prompt"] = handoff_prompt

    if action == "compact" and any(item.get("action") == "clear" for item in state["queue"]):
        return "A /clear request is already queued, so /compact was not added."

    if action == "clear":
        state["queue"] = [item for item in state["queue"] if item.get("action") != "compact"]
        state["pending_clear"] = None

    for item in state["queue"]:
        if item.get("action") == action:
            item["reason"] = reason
            item["queued_at"] = queued_at
            if thread_id:
                item["thread_id"] = thread_id
            elif action == "clear":
                item.pop("thread_id", None)
            if next_task:
                item["next_task"] = next_task
            elif action == "clear":
                item.pop("next_task", None)
            if handoff_prompt:
                item["handoff_prompt"] = handoff_prompt
            elif action == "clear":
                item.pop("handoff_prompt", None)
            return f"Updated queued /{action} request for the next completed-task checkpoint."

    state["queue"].append(request)
    return f"Queued /{action} for the next completed-task checkpoint."


def strongest_request(queue: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not queue:
        return None
    clear = [item for item in queue if item.get("action") == "clear"]
    if clear:
        return clear[-1]
    compact = [item for item in queue if item.get("action") == "compact"]
    if compact:
        return compact[-1]
    return queue[-1]


def emit_request(
    state: dict[str, Any], run_action: bool = False
) -> tuple[str, dict[str, Any] | None]:
    request = strongest_request(state["queue"])
    if request is None:
        return "No queued conversation maintenance.", None

    action = request.get("action", "compact")
    command = f"/{action}"
    if run_action and action == "compact":
        lines = [
            "Queued conversation maintenance is ready.",
            "Running /compact through Codex app-server.",
        ]
    elif run_action and action == "clear":
        lines = [
            "Queued conversation maintenance is ready.",
            "Seeding a fresh Codex thread with the handoff prompt for the task pivot.",
        ]
    else:
        lines = [
            "Queued conversation maintenance is ready.",
            f"Ask the user to send: {command}",
        ]
    if request.get("reason"):
        lines.append(f"Reason: {request['reason']}")
    if request.get("next_task"):
        lines.append(f"Next task: {request['next_task']}")
    if request.get("handoff_prompt"):
        lines.append("Handoff prompt: yes")
    if not (run_action and action in {"compact", "clear"}):
        lines.append("Note: agents cannot run chat slash commands from this script.")

    state["last_delivery"] = {
        "action": action,
        "reason": request.get("reason"),
        "delivered_at": now(),
    }
    if request.get("next_task"):
        state["last_delivery"]["next_task"] = request["next_task"]
    if request.get("handoff_prompt"):
        state["last_delivery"]["handoff_prompt"] = request["handoff_prompt"]
    if action == "clear" and not run_action:
        state["pending_clear"] = {
            "reason": request.get("reason"),
            "requested_at": state["last_delivery"]["delivered_at"],
            "next_task": request.get("next_task"),
            "handoff_prompt": request.get("handoff_prompt"),
        }
    state["queue"] = []
    return "\n".join(lines), request


def describe_task(task: dict[str, Any] | None) -> str:
    if not task:
        return "(none)"
    return task.get("label") or "(unnamed task)"


def mark_pending_clear(
    state: dict[str, Any],
    reason: str | None,
    previous_task: dict[str, Any] | None = None,
    next_task: str | None = None,
) -> str:
    requested_at = now()
    state["pending_clear"] = {
        "reason": reason,
        "previous_task": describe_task(previous_task),
        "next_task": next_task,
        "requested_at": requested_at,
    }
    state["last_delivery"] = {
        "action": "clear",
        "reason": reason,
        "delivered_at": requested_at,
    }
    state["active_task"] = None
    state["queue"] = []

    lines = [
        "Hard task boundary reached.",
        "Ask the user to send: /clear",
    ]
    if previous_task:
        lines.append(f"Previous task: {describe_task(previous_task)}")
    if next_task:
        lines.append(f"Next task: {next_task}")
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append("Continue only after the user sends /clear.")
    lines.append("Note: agents cannot run chat slash commands from this script.")
    return "\n".join(lines)


def cmd_start(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if state.get("pending_clear") and not args.after_clear:
        pending = state["pending_clear"]
        print("A /clear request is still pending from a task boundary.")
        print("Ask the user to send: /clear")
        if pending.get("reason"):
            print(f"Reason: {pending['reason']}")
        print("After the user sends /clear, run `ack-clear` or `start --after-clear`.")
        return 1

    task = state.get("active_task")
    if task and args.label and task.get("label") and task.get("label") != args.label:
        reason = f"Task changed from {describe_task(task)} to {args.label}"
        handoff = default_handoff_prompt(args.label, reason)
        state["active_task"] = None
        print(queue_action(state, "clear", reason, next_task=args.label, handoff_prompt=handoff))
        print("The Codex Stop hook will copy the handoff prompt and open the VS Code Codex new-thread route at the end of this turn.")
        save_state(args.state, state)
        return 0

    if args.after_clear:
        state["pending_clear"] = None

    state["active_task"] = {
        "label": args.label,
        "started_at": now(),
    }
    save_state(args.state, state)
    print(f"Marked task active: {args.label or '(unnamed task)'}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if state.get("pending_clear"):
        print("A /clear request is pending; not queuing /compact until the boundary is resolved.")
        print("Ask the user to send: /clear")
        return 1

    print(queue_action(state, args.action, args.reason, args.thread_id))
    if state["active_task"]:
        print("The Codex Stop hook will drain it when this turn/task completes.")
    else:
        print("The Codex Stop hook will drain it at the end of the current turn.")
    save_state(args.state, state)
    return 0


def cmd_pivot(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    reason = args.reason or "Task changed or hard pivot requested"
    handoff = resolve_handoff_prompt(
        args.next_task, reason, args.handoff, args.handoff_file, args.summary, args.criteria
    )
    state["active_task"] = None
    if getattr(args, "stage_feed", False):
        # Claude Code path: write the handoff to the feed now and get out of the way.
        # No queue (so the Stop hook won't try the Codex new-thread route) and no URL;
        # the user's /clear fires SessionStart, which injects the handoff and — when
        # that hook passes --auto-submit — self-starts the fresh thread.
        state["queue"] = []
        state["pending_clear"] = None
        feed_path = write_handoff_feed(args.feed, handoff)
        state["last_delivery"] = {
            "action": "clear",
            "reason": reason,
            "delivered_at": now(),
            "next_task": args.next_task,
            "handoff_prompt": handoff,
            "completed": True,
            "message": f"Staged handoff to feed {feed_path}",
            "source": "stage-feed",
        }
        print(f"Staged handoff to the feed: {feed_path}")
        print("Run /clear — the SessionStart hook injects it and self-starts the thread.")
        save_state(args.state, state)
        return 0
    if args.open_new_thread:
        state["queue"] = []
        state["pending_clear"] = None
        ok, message, thread_id = deliver_clear_handoff(
            handoff,
            feed=args.feed,
            auto_submit=args.auto_submit,
            focus_delay=args.focus_delay,
            restore_focus=getattr(args, "restore_focus", False),
        )
        print(message)
        state["last_delivery"] = {
            "action": "clear",
            "reason": reason,
            "delivered_at": now(),
            "next_task": args.next_task,
            "handoff_prompt": handoff,
            "completed": ok,
            "message": message,
            "source": "manual",
        }
        if thread_id:
            state["last_delivery"]["thread_id"] = thread_id
        if not ok:
            save_state(args.state, state)
            return 1
    else:
        print(queue_action(state, "clear", reason, next_task=args.next_task, handoff_prompt=handoff))
        print(
            "Queued. On this turn's Stop hook the Claude Code path (--clear-keystroke) "
            "writes the handoff to the feed and synthesizes /clear; SessionStart then "
            "injects it and self-starts the fresh thread. (Codex path: the Stop hook "
            "stages the feed for the next New Thread instead.)"
        )
    save_state(args.state, state)
    return 0


def cmd_ack_clear(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    had_pending = bool(state.get("pending_clear"))
    state["pending_clear"] = None

    if args.label:
        state["active_task"] = {
            "label": args.label,
            "started_at": now(),
        }
        print(f"Recorded /clear as complete. Marked task active: {args.label}")
    elif had_pending:
        print("Recorded /clear as complete.")
    else:
        print("No pending /clear was recorded.")

    save_state(args.state, state)
    return 0


def drain_finished_task(args: argparse.Namespace, source: str) -> int:
    state = load_state(args.state)
    task = state.get("active_task")
    request = strongest_request(state["queue"])

    if getattr(args, "dry_run", False):
        if task:
            print(f"{source} would mark task finished: {task.get('label') or '(unnamed task)'}")
        if request:
            print(f"{source} would drain queued /{request.get('action', 'compact')}.")
            if request.get("reason"):
                print(f"Reason: {request['reason']}")
            if request.get("handoff_prompt"):
                print("Handoff prompt: yes")
        else:
            print("No queued conversation maintenance.")
        return 0

    if source == "hook" and not task and request is None:
        return 0

    if task:
        if source == "hook":
            print(f"Stop hook reached after task/turn: {task.get('label') or '(unnamed task)'}")
        else:
            print(f"Marked task finished: {task.get('label') or '(unnamed task)'}")
    elif source != "hook":
        print("No active task was recorded.")
    state["active_task"] = None

    message, request = emit_request(state, run_action=args.run)
    if request or source != "hook":
        print(message)
    if args.run and request and request.get("action") == "compact":
        if getattr(args, "compact_keystroke", False):
            # Claude Code path: no app-server RPC exists, so deliver the queued
            # /compact by synthesizing keystrokes into the focused composer. Same
            # focus handling as the /clear path: with --grab-focus we activate VS
            # Code first; with --restore-focus we remember the app we interrupted
            # (only if it wasn't VS Code) and, once /compact is submitted, hand
            # focus straight back — there's no follow-up kickoff to defer to.
            grab = getattr(args, "grab_focus", False)
            restore = getattr(args, "restore_focus", False)
            if grab and restore:
                remember_prev_app()
            ok, run_message = send_slash_command_keys(
                getattr(args, "compact_command", "/compact"),
                focus_delay=getattr(args, "focus_delay", 0.5),
                submit_delay=getattr(args, "submit_delay", 1.0),
                grab_focus=grab,
            )
            if ok and grab and restore:
                _, restore_msg = restore_prev_app()
                run_message = f"{run_message} {restore_msg}"
            print(run_message)
            if state.get("last_delivery"):
                state["last_delivery"]["completed"] = ok
                state["last_delivery"]["source"] = source
                state["last_delivery"]["message"] = run_message
            if not ok:
                request["queued_at"] = now()
                state["queue"].append(request)
            save_state(args.state, state)
            return 0 if ok else 1
        thread_id = args.thread_id or request.get("thread_id")
        transport = args.transport
        if source == "hook" and transport == "proxy":
            transport = "auto"
        ok, run_message = compact_via_app_server(
            thread_id,
            transport=transport,
            sock=args.sock,
            timeout=args.timeout,
        )
        print(run_message)
        if state.get("last_delivery"):
            state["last_delivery"]["completed"] = ok
            state["last_delivery"]["thread_id"] = thread_id
            state["last_delivery"]["source"] = source
            state["last_delivery"]["message"] = run_message
        if not ok:
            request["queued_at"] = now()
            state["queue"].append(request)
        refresh_ok = record_ui_refresh(state, compact_ui_refresh(args, thread_id)) if ok else True
        save_state(args.state, state)
        return 0 if ok and refresh_ok else 1
    if args.run and request and request.get("action") == "clear":
        prompt = request.get("handoff_prompt") or default_handoff_prompt(
            request.get("next_task"),
            request.get("reason"),
        )
        if getattr(args, "clear_keystroke", False):
            # Claude Code hands-off pivot: write the handoff to the feed *now*
            # (so the current session's UserPromptSubmit can't consume it first),
            # then synthesize /clear. That fires SessionStart(source=clear), whose
            # hook injects the feed and spawns the `go` kickoff — self-starting the
            # fresh thread with no user interaction.
            write_handoff_feed(args.feed, prompt)
            # Record the app we're stealing focus from (only if it isn't VS Code),
            # so the kickoff can hand focus back once the new conversation starts.
            if getattr(args, "restore_focus", False) and getattr(args, "grab_focus", False):
                remember_prev_app()
            ok, run_message = send_slash_command_keys(
                getattr(args, "clear_command", "/clear"),
                focus_delay=getattr(args, "focus_delay", 0.5),
                submit_delay=getattr(args, "submit_delay", 1.0),
                grab_focus=getattr(args, "grab_focus", False),
            )
            print(run_message)
            if state.get("last_delivery"):
                state["last_delivery"]["completed"] = ok
                state["last_delivery"]["source"] = source
                state["last_delivery"]["message"] = run_message
                if request.get("next_task"):
                    state["last_delivery"]["next_task"] = request["next_task"]
                state["last_delivery"]["handoff_prompt"] = prompt
            if not ok:
                request["queued_at"] = now()
                state["queue"].append(request)
            save_state(args.state, state)
            return 0 if ok else 1
        ok, run_message, thread_id = deliver_clear_handoff(
            prompt,
            feed=args.feed,
            auto_submit=getattr(args, "auto_submit", None),
            focus_delay=getattr(args, "focus_delay", 5.0),
            restore_focus=getattr(args, "restore_focus", False),
        )
        print(run_message)
        if state.get("last_delivery"):
            state["last_delivery"]["completed"] = ok
            state["last_delivery"]["source"] = source
            state["last_delivery"]["message"] = run_message
            if request.get("next_task"):
                state["last_delivery"]["next_task"] = request["next_task"]
            state["last_delivery"]["handoff_prompt"] = prompt
            if thread_id:
                state["last_delivery"]["thread_id"] = thread_id
        if not ok:
            request["queued_at"] = now()
            state["queue"].append(request)
        save_state(args.state, state)
        return 0 if ok else 1
    save_state(args.state, state)
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    fd = acquire_lock(args.lock)
    if fd is None:
        return 1
    try:
        return drain_finished_task(args, "finish")
    finally:
        release_lock(args.lock, fd)


def cmd_drain_hook(args: argparse.Namespace) -> int:
    fd = acquire_lock(args.lock)
    if fd is None:
        return 0
    try:
        return drain_finished_task(args, "hook")
    finally:
        release_lock(args.lock, fd)


def cmd_compact_now(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if state.get("active_task") and not args.force:
        print(
            queue_action(
                state,
                "compact",
                args.reason or "compact-now requested during task",
                args.thread_id,
            )
        )
        print("A task is still active, so compact was queued instead of run.")
        print("The Codex Stop hook will drain it when this turn/task completes.")
        save_state(args.state, state)
        return 0

    ok, message = compact_via_app_server(
        args.thread_id,
        transport=args.transport,
        sock=args.sock,
        timeout=args.timeout,
    )
    print(message)
    if ok:
        state["last_delivery"] = {
            "action": "compact",
            "reason": args.reason or "compact-now",
            "delivered_at": now(),
            "thread_id": args.thread_id,
            "completed": True,
            "message": message,
            "source": "manual",
        }
        refresh_ok = record_ui_refresh(state, compact_ui_refresh(args, args.thread_id))
        save_state(args.state, state)
        return 0 if refresh_ok else 1
    return 1


def cmd_refresh_ui(args: argparse.Namespace) -> int:
    restore_thread_id = args.thread_id if getattr(args, "restore_thread", True) else None
    ok, message = refresh_vscode_codex_ui(
        timeout=args.refresh_timeout,
        fallback=args.refresh_fallback,
        mode=args.refresh_mode,
        restore_thread_id=restore_thread_id,
        restore_delay=args.restore_delay,
    )
    print(message)
    return 0 if ok else 1


def cmd_emit(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if state["active_task"] and not args.force:
        print("A task is still marked active; keeping the request queued.")
        print("Run `finish` when the task is complete, or use `emit --force`.")
        return 1

    message, _request = emit_request(state)
    print(message)
    save_state(args.state, state)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    task = state.get("active_task")
    if task:
        print(f"Active task: {task.get('label') or '(unnamed task)'}")
        print(f"Started at: {task.get('started_at')}")
    else:
        print("Active task: none")

    pending = state.get("pending_clear")
    if pending:
        print("Pending clear: yes")
        print(f"Requested at: {pending.get('requested_at')}")
        if pending.get("previous_task"):
            print(f"Previous task: {pending['previous_task']}")
        if pending.get("next_task"):
            print(f"Next task: {pending['next_task']}")
        if pending.get("handoff_prompt"):
            print("Handoff prompt: yes")
        if pending.get("reason"):
            print(f"Reason: {pending['reason']}")
    else:
        print("Pending clear: no")

    if state["queue"]:
        print("Queued requests:")
        for item in state["queue"]:
            reason = f" - {item['reason']}" if item.get("reason") else ""
            thread = f" [thread {item['thread_id']}]" if item.get("thread_id") else ""
            next_task = f" [next: {item['next_task']}]" if item.get("next_task") else ""
            handoff = " [handoff]" if item.get("handoff_prompt") else ""
            print(
                f"  /{item.get('action', 'compact')} queued at "
                f"{item.get('queued_at')}{thread}{next_task}{handoff}{reason}"
            )
    else:
        print("Queued requests: none")

    last = state.get("last_delivery")
    if last:
        details = []
        if last.get("thread_id"):
            details.append(f"thread {last['thread_id']}")
        if last.get("next_task"):
            details.append(f"next {last['next_task']}")
        if last.get("handoff_prompt"):
            details.append("handoff=True")
        if "completed" in last:
            details.append(f"completed={last['completed']}")
        if "ui_refresh" in last:
            details.append(f"ui_refresh={last['ui_refresh']}")
        suffix = f" ({', '.join(details)})" if details else ""
        print(f"Last delivered: /{last.get('action')} at {last.get('delivered_at')}{suffix}")
        if last.get("message"):
            print(f"Last message: {last['message']}")
        if last.get("ui_refresh_message"):
            print(f"Last UI refresh: {last['ui_refresh_message']}")

    feed_text = read_handoff_feed(args.feed)
    if feed_text is not None:
        preview = feed_text.strip().splitlines()[0] if feed_text.strip() else ""
        if len(preview) > 72:
            preview = preview[:69] + "..."
        print(f"Handoff feed: {args.feed} ({preview})")
    else:
        print("Handoff feed: none")
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    feed_text = read_handoff_feed(args.feed)
    if args.clear:
        try:
            args.feed.unlink()
            print(f"Cleared the handoff feed: {args.feed}")
        except FileNotFoundError:
            print("No handoff feed to clear.")
        return 0
    if args.emit_context:
        # Invoked by a Codex hook (SessionStart or UserPromptSubmit). stdout must
        # be the hook protocol JSON (or empty), so emit `additionalContext` only
        # when a handoff is pending, then consume the feed so it injects once.
        # `hookEventName` must match the event that fired (see --event).
        if feed_text is None:
            return 0
        payload = {
            "hookSpecificOutput": {
                "hookEventName": args.event,
                "additionalContext": feed_text.rstrip("\n"),
            }
        }
        print(json.dumps(payload))
        try:
            args.feed.unlink()
        except FileNotFoundError:
            pass
        # Claude Code self-start: after injecting the handoff, optionally synthesize
        # a kickoff message (e.g. "go") into the reset composer so the fresh thread
        # begins its autonomous loop with no user interaction. Detached so this hook
        # returns immediately and Claude Code processes the additionalContext first.
        auto_submit = getattr(args, "auto_submit", None)
        if auto_submit:
            spawn_detached_kickoff(
                auto_submit,
                getattr(args, "focus_delay", 3.0),
                grab_focus=getattr(args, "grab_focus", False),
                restore_focus=getattr(args, "restore_focus", False),
            )
        return 0
    if feed_text is None:
        print(f"No handoff feed at {args.feed}.")
        return 1
    if args.path:
        print(str(args.feed))
        return 0
    if args.show:
        sys.stdout.write(feed_text if feed_text.endswith("\n") else feed_text + "\n")
        return 0

    # Default action: (re)copy the saved handoff prompt to the clipboard.
    ok, message = copy_to_clipboard(feed_text.rstrip("\n"))
    print(message)
    if ok:
        print(f"Source: {args.feed}")
    return 0 if ok else 1


def cmd_kickoff(args: argparse.Namespace) -> int:
    """Type the kickoff message into the focused Claude Code composer.

    Spawned detached by the SessionStart hook (see spawn_detached_kickoff) so a
    fresh thread self-starts after `/clear`. Best-effort; exit status reflects
    whether keystrokes were actually sent.
    """
    ok, message = send_claude_kickoff_keys(
        args.text,
        args.focus_delay,
        grab_focus=getattr(args, "grab_focus", False),
        restore_focus=getattr(args, "restore_focus", False),
        restore_delay=getattr(args, "restore_delay", 2.5),
    )
    print(message)
    return 0 if ok else 1


def cmd_cancel(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if args.action == "all":
        removed = len(state["queue"])
        state["queue"] = []
        if state.get("pending_clear"):
            state["pending_clear"] = None
            removed += 1
    else:
        before = len(state["queue"])
        state["queue"] = [item for item in state["queue"] if item.get("action") != args.action]
        removed = before - len(state["queue"])
        if args.action == "clear" and state.get("pending_clear"):
            state["pending_clear"] = None
            removed += 1
    save_state(args.state, state)
    print(f"Removed {removed} queued request(s).")
    return 0


def add_auto_submit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auto-submit",
        metavar="TEXT",
        dest="auto_submit",
        default=None,
        help=(
            "When draining a queued /clear, open a fresh Codex chat and synthesize "
            "TEXT + Enter (e.g. 'go') into the auto-focused composer so the new thread "
            "starts hands-off. Only types when VS Code is frontmost."
        ),
    )
    parser.add_argument(
        "--focus-delay",
        type=float,
        default=5.0,
        dest="focus_delay",
        help="Seconds to wait for the new Codex composer to focus before auto-typing. Default: 5.",
    )


def add_keystroke_compact_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compact-keystroke",
        action="store_true",
        dest="compact_keystroke",
        help=(
            "Deliver a queued /compact by synthesizing keystrokes into the focused "
            "Claude Code composer (for the Claude Code Stop hook) instead of the Codex "
            "app-server. Only types when VS Code is frontmost."
        ),
    )
    parser.add_argument(
        "--compact-command",
        default="/compact",
        dest="compact_command",
        help="Slash command typed when --compact-keystroke is set. Default: /compact.",
    )
    parser.add_argument(
        "--submit-delay",
        type=float,
        default=1.0,
        dest="submit_delay",
        help=(
            "Seconds to wait after typing the slash command (for the autocomplete "
            "menu to settle) before pressing Enter to submit. Default: 1.0."
        ),
    )
    parser.add_argument(
        "--clear-keystroke",
        action="store_true",
        dest="clear_keystroke",
        help=(
            "Deliver a queued /clear by writing the handoff to the feed and then "
            "synthesizing keystrokes into the focused Claude Code composer (Claude Code "
            "Stop hook path) instead of the Codex new-thread route. SessionStart then "
            "injects the handoff and self-starts the fresh thread. Only types when VS "
            "Code is frontmost."
        ),
    )
    parser.add_argument(
        "--clear-command",
        default="/clear",
        dest="clear_command",
        help="Slash command typed when --clear-keystroke is set. Default: /clear.",
    )
    parser.add_argument(
        "--grab-focus",
        action="store_true",
        dest="grab_focus",
        help=(
            "Before synthesizing keystrokes, activate VS Code so they land even if the "
            "user was in another app. The frontmost guard still runs afterwards."
        ),
    )
    parser.add_argument(
        "--restore-focus",
        action="store_true",
        dest="restore_focus",
        help=(
            "With --grab-focus on a /clear pivot: remember the app focus was taken from "
            "(only when it wasn't VS Code) so the kickoff hands focus back once the new "
            "conversation starts."
        ),
    )


def add_compact_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--thread-id",
        default=os.environ.get("CODEX_THREAD_ID"),
        help="Codex thread id to compact. Default: CODEX_THREAD_ID.",
    )
    parser.add_argument(
        "--transport",
        choices=["auto", "proxy", "stdio"],
        default="auto",
        help=(
            "App-server transport. `auto` tries proxy, then stdio; "
            "`proxy` requires a live control socket; `stdio` starts a temporary app-server."
        ),
    )
    parser.add_argument("--sock", type=Path, help="Optional app-server Unix socket path.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for the app-server compact request.",
    )


def add_refresh_runtime_args(parser: argparse.ArgumentParser) -> None:
    refresh = parser.add_mutually_exclusive_group()
    refresh.add_argument(
        "--refresh-ui",
        action="store_true",
        dest="refresh_ui",
        default=True,
        help="Refresh the VS Code Codex UI after a successful compact. Default: enabled.",
    )
    refresh.add_argument(
        "--no-refresh-ui",
        action="store_false",
        dest="refresh_ui",
        help="Do not refresh the VS Code UI after compaction.",
    )
    add_refresh_command_args(parser)


def add_refresh_command_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh-mode",
        choices=["window", "webview"],
        default="window",
        help=(
            "VS Code UI refresh mode. Default: window. "
            "`webview` is opt-in because it can leave Codex stuck at the logo."
        ),
    )
    parser.add_argument(
        "--no-refresh-fallback",
        action="store_false",
        dest="refresh_fallback",
        default=True,
        help="When --refresh-mode webview fails, do not fall back to reloading the window.",
    )
    parser.add_argument(
        "--refresh-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait while automating the VS Code refresh command. Default: 15.",
    )
    restore = parser.add_mutually_exclusive_group()
    restore.add_argument(
        "--restore-thread",
        action="store_true",
        dest="restore_thread",
        default=True,
        help=(
            "After a window refresh, reopen the compacted Codex thread when a thread id "
            "is available. Default: enabled."
        ),
    )
    restore.add_argument(
        "--no-restore-thread",
        action="store_false",
        dest="restore_thread",
        help="Do not reopen the Codex thread after a window refresh.",
    )
    parser.add_argument(
        "--restore-delay",
        type=float,
        default=3.0,
        help="Seconds to wait after requesting a window reload before reopening the thread. Default: 3.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Queue /compact after tasks and open VS Code Codex new-thread routes at task pivots."
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        help=f"State file path. Default: {DEFAULT_STATE}",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=DEFAULT_LOCK,
        help=f"Drain lock path. Default: {DEFAULT_LOCK}",
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=DEFAULT_FEED,
        help=f"Sidecar handoff prompt feed file. Default: {DEFAULT_FEED}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Mark an agent task as active.")
    start.add_argument("label", nargs="?", help="Optional task label.")
    start.add_argument(
        "--after-clear",
        action="store_true",
        help="Acknowledge that the user already sent /clear, then start.",
    )
    start.set_defaults(func=cmd_start)

    compact = subparsers.add_parser("compact", help="Queue /compact after the active task.")
    compact.add_argument("--reason", help="Optional reason shown at delivery.")
    compact.add_argument(
        "--thread-id",
        default=os.environ.get("CODEX_THREAD_ID"),
        help="Codex thread id to compact. Default: CODEX_THREAD_ID.",
    )
    compact.set_defaults(func=cmd_queue, action="compact")

    compact_now = subparsers.add_parser(
        "compact-now",
        help="Request Codex app-server compaction when no task is active.",
    )
    add_compact_runtime_args(compact_now)
    add_refresh_runtime_args(compact_now)
    compact_now.add_argument(
        "--force",
        action="store_true",
        help="Run immediately even if a task is marked active.",
    )
    compact_now.add_argument("--reason", help="Optional reason recorded in state.")
    compact_now.set_defaults(func=cmd_compact_now)

    clear = subparsers.add_parser(
        "clear",
        aliases=["pivot", "switch"],
        help="Queue the VS Code Codex new-thread route for a new task or hard pivot.",
    )
    clear.add_argument("next_task", nargs="?", help="Optional label for the next task.")
    clear.add_argument("--reason", help="Optional reason shown to the user.")
    clear.add_argument(
        "--handoff",
        help="Full handoff prompt text (overrides the built-in template).",
    )
    clear.add_argument(
        "--summary",
        help="Short summary of what the previous conversation was about (context for the default handoff).",
    )
    clear.add_argument(
        "--criteria",
        help="Ironclad, measurable success criteria carried over from the user's original request.",
    )
    clear.add_argument(
        "--focus-delay",
        type=float,
        default=5.0,
        dest="focus_delay",
        help="Seconds to wait for the new Codex composer to focus before auto-typing. Default: 5.",
    )
    clear.add_argument(
        "--handoff-file",
        type=Path,
        help="Read the fresh-thread handoff prompt from this UTF-8 text file.",
    )
    clear.add_argument(
        "--open-new-thread",
        action="store_true",
        help="Open the VS Code Codex new-thread route immediately instead of queueing for the Stop hook.",
    )
    clear.add_argument(
        "--stage-feed",
        action="store_true",
        dest="stage_feed",
        help=(
            "Claude Code path: write the handoff straight to the feed now (no queue, no "
            "Codex new-thread URL). The user runs /clear and the SessionStart hook injects "
            "it — with --auto-submit on that hook, the fresh thread self-starts."
        ),
    )
    clear.add_argument(
        "--auto-submit",
        metavar="TEXT",
        help=(
            "With --open-new-thread: after staging the handoff, synthesize keystrokes "
            "(TEXT + Enter, e.g. 'go') into the auto-focused Codex composer to start the "
            "thread hands-off. Only types when VS Code is frontmost."
        ),
    )
    clear.add_argument(
        "--restore-focus",
        action="store_true",
        dest="restore_focus",
        help=(
            "With --open-new-thread --auto-submit: after the new Codex thread starts, hand "
            "focus back to the app that was frontmost before (only if it wasn't VS Code)."
        ),
    )
    clear.set_defaults(func=cmd_pivot)

    ack_clear = subparsers.add_parser(
        "ack-clear",
        aliases=["cleared"],
        help="Record that the user sent /clear and optionally start the next task.",
    )
    ack_clear.add_argument("label", nargs="?", help="Optional next task label.")
    ack_clear.set_defaults(func=cmd_ack_clear)

    finish = subparsers.add_parser(
        "finish",
        aliases=["done", "complete"],
        help="Mark the task complete and run queued maintenance.",
    )
    finish.add_argument(
        "--run",
        action="store_true",
        dest="run",
        help="Run queued /compact through app-server. This is the default.",
    )
    finish.add_argument(
        "--no-run",
        action="store_false",
        dest="run",
        help="Only emit the queued request instead of running it.",
    )
    add_compact_runtime_args(finish)
    add_refresh_runtime_args(finish)
    add_auto_submit_args(finish)
    add_keystroke_compact_args(finish)
    finish.set_defaults(func=cmd_finish, run=True)

    drain_hook = subparsers.add_parser(
        "drain-hook",
        help="Drain queued maintenance from a Codex Stop hook.",
    )
    drain_hook.add_argument(
        "--no-run",
        action="store_false",
        dest="run",
        help="Only emit the queued request instead of running it.",
    )
    drain_hook.add_argument(
        "--dry-run",
        action="store_true",
        help="Describe what the hook would drain without changing state.",
    )
    add_compact_runtime_args(drain_hook)
    add_refresh_runtime_args(drain_hook)
    add_auto_submit_args(drain_hook)
    add_keystroke_compact_args(drain_hook)
    drain_hook.set_defaults(func=cmd_drain_hook, run=True)

    refresh_ui = subparsers.add_parser(
        "refresh-ui",
        help="Refresh the VS Code Codex UI using guarded Command Palette automation.",
    )
    refresh_ui.add_argument(
        "--thread-id",
        default=os.environ.get("CODEX_THREAD_ID"),
        help="Codex thread id to restore after a window refresh. Default: CODEX_THREAD_ID.",
    )
    add_refresh_command_args(refresh_ui)
    refresh_ui.set_defaults(func=cmd_refresh_ui)

    emit = subparsers.add_parser("emit", help="Emit a queued request if no task is active.")
    emit.add_argument("--force", action="store_true", help="Emit even if a task is active.")
    emit.set_defaults(func=cmd_emit)

    status = subparsers.add_parser("status", help="Show active task and queued requests.")
    status.set_defaults(func=cmd_status)

    handoff = subparsers.add_parser(
        "handoff",
        help="Work with the saved handoff feed (re-copy, show, locate, or clear it).",
    )
    handoff_mode = handoff.add_mutually_exclusive_group()
    handoff_mode.add_argument(
        "--copy",
        action="store_true",
        help="Copy the saved handoff prompt to the clipboard (default).",
    )
    handoff_mode.add_argument(
        "--show",
        action="store_true",
        help="Print the saved handoff prompt to stdout.",
    )
    handoff_mode.add_argument(
        "--path",
        action="store_true",
        help="Print the handoff feed file path.",
    )
    handoff_mode.add_argument(
        "--emit-context",
        action="store_true",
        dest="emit_context",
        help="Emit the saved handoff as Codex hook JSON, then consume it.",
    )
    handoff.add_argument(
        "--event",
        choices=["SessionStart", "UserPromptSubmit"],
        default="SessionStart",
        help="Hook event name to tag emitted --emit-context output with.",
    )
    handoff.add_argument(
        "--auto-submit",
        metavar="TEXT",
        dest="auto_submit",
        default=None,
        help=(
            "With --emit-context (Claude Code SessionStart on /clear): after injecting "
            "the handoff, synthesize TEXT + Enter (e.g. 'go') into the reset composer so "
            "the fresh thread self-starts its autonomous loop. Only types when VS Code is "
            "frontmost; skipped when no handoff is pending."
        ),
    )
    handoff.add_argument(
        "--focus-delay",
        type=float,
        default=3.0,
        dest="focus_delay",
        help="Seconds to wait for the reset Claude composer to focus before auto-typing. Default: 3.",
    )
    handoff.add_argument(
        "--grab-focus",
        action="store_true",
        dest="grab_focus",
        help="Pass through to the kickoff so it activates VS Code before typing the kickoff message.",
    )
    handoff.add_argument(
        "--restore-focus",
        action="store_true",
        dest="restore_focus",
        help="Pass through to the kickoff so it hands focus back to the interrupted app after the new conversation starts.",
    )
    handoff_mode.add_argument(
        "--clear",
        action="store_true",
        help="Delete the saved handoff feed.",
    )
    handoff.set_defaults(func=cmd_handoff)

    kickoff = subparsers.add_parser(
        "kickoff",
        help="Type a kickoff message into the focused Claude Code composer (used by the SessionStart hook).",
    )
    kickoff.add_argument("--text", default="go", help="Kickoff text to type + Enter. Default: go.")
    kickoff.add_argument(
        "--focus-delay",
        type=float,
        default=3.0,
        dest="focus_delay",
        help="Seconds to wait before typing so the reset composer can focus. Default: 3.",
    )
    kickoff.add_argument(
        "--grab-focus",
        action="store_true",
        dest="grab_focus",
        help="Activate VS Code before typing so the kickoff lands even if the user switched apps.",
    )
    kickoff.add_argument(
        "--restore-focus",
        action="store_true",
        dest="restore_focus",
        help="After typing, hand focus back to the app the /clear pivot interrupted (if it wasn't VS Code).",
    )
    kickoff.add_argument(
        "--restore-delay",
        type=float,
        default=2.5,
        dest="restore_delay",
        help="Seconds to wait after typing (for the new conversation to start) before restoring focus. Default: 2.5.",
    )
    kickoff.set_defaults(func=cmd_kickoff)

    cancel = subparsers.add_parser("cancel", help="Remove queued requests.")
    cancel.add_argument("action", choices=["compact", "clear", "all"], nargs="?", default="all")
    cancel.set_defaults(func=cmd_cancel)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state = args.state.expanduser()
    args.lock = args.lock.expanduser()
    args.feed = args.feed.expanduser()
    if hasattr(args, "thread_id") and not args.thread_id:
        args.thread_id = thread_id_from_stdin()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
