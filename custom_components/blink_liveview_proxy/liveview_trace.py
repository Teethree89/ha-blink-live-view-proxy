"""A record of one live view's path, and a verdict on where it stopped.

Issue #91 took three rounds of screenshots and logs to establish one fact: the
iPhone loaded the playlist and then never asked for a segment. The add-on log
could not say so on its own, because it only sees what gets past the
integration, and the integration kept no record at all. A refused request was
invisible to both.

So the integration notes every request a live view makes of it - the playlist,
each segment, the MPEG-TS stream, the stop - keyed by the player's session id.
When a live view fails, the player sends its own timeline of what the video
element did, and the two together give a verdict on where the path stopped.
The report is plain text for pasting into an issue: tokens never enter it, and
an address is reduced to the kind of address it is.

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Callable

# A handful of recent live views, in memory only. Diagnostics are wanted
# straight after a failure, not days later, and nothing here is worth a write.
MAX_SESSIONS = 20
# Sessions no request has yet proved with a valid token. Requests are recorded
# before authorisation on purpose (a refused request is the one nothing else
# shows), but that means anyone who can reach Home Assistant can make them,
# with no token at all. So they wait here, apart: never listed in the admin
# panel, and unable to push a real live view out of the trace. A session moves
# across the moment one of its requests is authorised.
MAX_UNVERIFIED_SESSIONS = 10
MAX_REQUESTS = 400
MAX_CLIENT_EVENTS = 300
# The player generates a UUID; anything else is not a session this page made.
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_PLAYLIST_SEGMENT_RE = re.compile(r"^[^#\s]")

NETWORK_STATES = ("EMPTY", "IDLE", "LOADING", "NO_SOURCE")
READY_STATES = ("NOTHING", "METADATA", "CURRENT_DATA", "FUTURE_DATA", "ENOUGH_DATA")


def valid_session(session: str | None) -> bool:
    """Whether a session id is one the player could have generated."""
    return bool(session) and bool(_SESSION_RE.match(session or ""))


def playlist_segment_count(text: str) -> int:
    """Count the media segments an HLS playlist lists."""
    return sum(1 for line in text.splitlines() if _PLAYLIST_SEGMENT_RE.match(line))


class LiveviewTrace:
    """Requests that reached the integration, per live-view session."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._wall = wall
        # Sessions with at least one authorised request.
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Sessions with none yet; see MAX_UNVERIFIED_SESSIONS.
        self._unverified: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _new_entry(self, slug: str) -> dict[str, Any]:
        return {
            "slug": slug,
            "started_mono": self._clock(),
            "started_wall": self._wall(),
            "requests": [],
            "dropped": 0,
        }

    @staticmethod
    def _add(table: OrderedDict[str, dict[str, Any]], session: str,
             entry: dict[str, Any], cap: int) -> None:
        table[session] = entry
        while len(table) > cap:
            table.popitem(last=False)

    def _session(self, session: str, slug: str) -> dict[str, Any]:
        for table in (self._sessions, self._unverified):
            entry = table.get(session)
            if entry is not None:
                table.move_to_end(session)
                return entry
        # New sessions start unverified: whoever made this request may hold
        # no token at all.
        entry = self._new_entry(slug)
        self._add(self._unverified, session, entry, MAX_UNVERIFIED_SESSIONS)
        return entry

    def verify(self, session: str | None, slug: str,
               record: dict[str, Any] | None = None) -> None:
        """Mark a session as a real live view: one of its requests was authorised.

        `record` is that request's record. If a flood of unverified sessions
        pushed this one out while the request was in flight, the session is
        started again from it rather than lost.
        """
        if not valid_session(session):
            return
        if session in self._sessions:
            self._sessions.move_to_end(session)
            return
        entry = self._unverified.pop(session, None)
        if entry is None:
            entry = self._new_entry(slug)
            if record is not None:
                record["at"] = 0.0
                entry["requests"].append(record)
        self._add(self._sessions, session, entry, MAX_SESSIONS)

    def begin(self, session: str | None, slug: str, kind: str, name: str = "") -> dict[str, Any] | None:
        """Note that a request arrived. Returns the record to finish, or None.

        Returns None for a request with no usable session id, so callers never
        need to check before finishing it.
        """
        if not valid_session(session):
            return None
        entry = self._session(session, slug)
        record = {
            "at": self._clock() - entry["started_mono"],
            "kind": kind,
            # A segment's file name comes from the URL, from a request that
            # may hold no token: one line, or it could forge lines in a report.
            "name": _clean(name, 80),
            "status": None,
            "ms": None,
            "bytes": 0,
            "detail": "",
            "_t0": self._clock(),
        }
        if len(entry["requests"]) >= MAX_REQUESTS:
            entry["dropped"] += 1
            return None
        entry["requests"].append(record)
        return record

    def finish(self, record: dict[str, Any] | None, status: int, detail: str = "") -> None:
        """Record how a request ended."""
        if record is None:
            return
        record["status"] = status
        record["ms"] = round((self._clock() - record["_t0"]) * 1000)
        if detail:
            record["detail"] = _clean(detail, 120)

    @staticmethod
    def add_bytes(record: dict[str, Any] | None, count: int) -> None:
        if record is not None:
            record["bytes"] += count

    def get(self, session: str) -> dict[str, Any] | None:
        """One session, verified or not.

        Only ever asked for by an authorised diagnostics request naming the
        session id itself - a random UUID - so an unverified session reaches
        the one device that made it, and is how a playlist refused on a stale
        token still gets its verdict.
        """
        return self._sessions.get(session) or self._unverified.get(session)

    def sessions(self) -> list[tuple[str, dict[str, Any]]]:
        """Every verified session still held, oldest first.

        This is what the admin panel lists, so unverified sessions are left
        out: anyone can make those, without a token.
        """
        return list(self._sessions.items())

    def forget(self, slug: str) -> None:
        """Drop one camera's sessions, so a cleared history stays cleared."""
        for table in (self._sessions, self._unverified):
            for session, entry in list(table.items()):
                if entry["slug"] == slug:
                    del table[session]


# --------------------------------------------------------------------------
# The verdict


def _ok(record: dict[str, Any]) -> bool:
    return record.get("status") in (200, 206)


def _status_hint(status: int | None) -> str:
    return {
        401: "the proxy rejected the integration's token",
        403: "the player's access token was refused; refresh the dashboard",
        404: "the camera or the stream was not found",
        429: "Blink's live-view cooldown is active; wait and try again",
        502: "the add-on did not answer, or failed",
        503: "the add-on is not ready (signing in to Blink, or reconnecting)",
    }.get(status or 0, "")


def _refused(records: list[dict[str, Any]], what: str) -> str:
    statuses = sorted({r.get("status") for r in records if r.get("status") is not None})
    if not statuses:
        return f"Home Assistant received the {what} request but it never finished."
    shown = ", ".join(str(s) for s in statuses)
    hint = _status_hint(statuses[-1])
    return f"Home Assistant refused the {what} (HTTP {shown})" + (f": {hint}." if hint else ".")


def verdict(server: dict[str, Any] | None, client: dict[str, Any]) -> str:
    """One sentence on where the live view's path stopped."""
    requests = (server or {}).get("requests", [])
    transport = client.get("transport") or ""
    played = bool(client.get("played"))
    buffered = bool(client.get("buffered"))
    code = safe_code(client.get("code"))

    if code == "E-001b":
        return "This browser can play neither stream type; nothing was requested."
    if code == "E-008":
        return "The browser waited for a tap to start playback, and none came."
    # A report the device could not send because Home Assistant did not
    # answer at all (not one it answered with an error: that report waits on
    # the device too, but Home Assistant was there, and what it saw stands).
    # That comes first: what Home Assistant saw of
    # such a live view - a playlist served and then nothing - is the same
    # shape as a device that stopped asking (#91), and reads as the device's
    # fault in a way that sends the reader the wrong way. Found by starting a
    # live view and switching on airplane mode while it loaded.
    lost = bool(client.get("queued")) and bool(client.get("unreachable"))
    if played:
        if lost:
            return ("The live view played, then the device lost its connection to Home "
                    "Assistant; its report was sent once the connection came back.")
        return "The live view played." + (
            f" It then ended with {code}." if code else ""
        )
    if lost:
        return ("The device could not reach Home Assistant when the live view failed; its "
                "report was sent once the connection came back. The connection broke while "
                "the live view was starting: check the device's network, or whether Home "
                "Assistant was restarting.")

    if transport == "hls":
        every_playlist = [r for r in requests if r["kind"] == "playlist"]
        # The page's own warm-up fetch is not the video player asking (#91).
        playlists = [r for r in every_playlist if r.get("name") != "warm-up"]
        segments = [r for r in requests if r["kind"] == "segment"]
        if not every_playlist:
            return ("No playlist request reached Home Assistant. The device could not "
                    "reach it at this address, or the player never asked.")
        if not any(_ok(r) for r in every_playlist):
            return _refused(every_playlist, "playlist")
        if not playlists:
            return ("The page started the stream, but the device's video player never "
                    "asked for it.")
        good = [r for r in playlists if _ok(r)]
        if not good:
            return _refused(playlists, "playlist")
        if not segments:
            if len(good) > 2:
                return ("The device kept reloading the playlist but never requested a "
                        "segment. The playlist may not have had enough video to start from.")
            return ("The device received the playlist and then stopped asking: no segment "
                    "request and no playlist reload reached Home Assistant. The cause is on "
                    "the device (see issue #91), not the camera or the proxy.")
        failed = [r for r in segments if not _ok(r)]
        if failed and len(failed) == len(segments):
            return _refused(failed, "segments")
        if buffered:
            return ("Video reached the device but never started playing: a decoding or "
                    "start-up problem on the device.")
        if failed:
            return (f"{len(failed)} of {len(segments)} segment requests failed, and nothing "
                    "was buffered.")
        return ("Segments were delivered, but the device reports nothing buffered. It may "
                "have discarded them as unplayable.")

    if transport == "mse":
        streams = [r for r in requests if r["kind"] == "mpegts"]
        if not streams:
            return ("No stream request reached Home Assistant. The device could not reach "
                    "it at this address, or the player never asked.")
        good = [r for r in streams if _ok(r)]
        if not good:
            return _refused(streams, "stream")
        sent = sum(r.get("bytes", 0) for r in good)
        if sent == 0:
            return ("Home Assistant accepted the stream but the add-on sent no video. "
                    "The camera may not have started; check the add-on log.")
        if buffered or client.get("bytes_arrived"):
            return ("Video reached the browser but never started playing: a decoding or "
                    "start-up problem in the browser.")
        return ("Home Assistant sent video, but the browser reports receiving none.")

    return "The player did not say which stream type it used."


def infer_transport(server: dict[str, Any] | None) -> str:
    """Which player a session used, judged from what it requested."""
    kinds = {record["kind"] for record in (server or {}).get("requests", [])}
    if kinds & {"playlist", "segment"}:
        return "hls"
    if "mpegts" in kinds:
        return "mse"
    return ""


def server_summary(server: dict[str, Any]) -> str:
    """What Home Assistant saw of a live view the device never reported on.

    No verdict: a live view that played and one whose device dropped off the
    network both end without a report, and only the device could tell them
    apart. This says what arrived, which is usually enough to see which.
    """
    requests = server.get("requests", [])
    last = max((record["at"] for record in requests), default=0.0)
    over = f" over {last:.0f} s" if last >= 1 else ""
    stopped = any(record["kind"] == "stop" for record in requests)
    tail = (" The player closed it." if stopped
            else " The player never closed it, so the device may have lost its connection.")
    prefix = "No report from the device. "

    def plural(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"

    if infer_transport(server) == "hls":
        playlists = [r for r in requests if r["kind"] == "playlist"]
        segments = [r for r in requests if r["kind"] == "segment"]
        if playlists and not any(_ok(r) for r in playlists):
            return prefix + _refused(playlists, "playlist") + tail
        served = [r for r in playlists + segments if _ok(r)]
        text = (f"Home Assistant served {plural(len([r for r in served if r['kind'] == 'playlist']), 'playlist')}"
                f" and {plural(len([r for r in served if r['kind'] == 'segment']), 'segment')}{over}")
        refused = [r for r in playlists + segments if r.get("status") is not None and not _ok(r)]
        if refused:
            text += f", and refused {len(refused)}"
        return prefix + text + "." + tail
    if infer_transport(server) == "mse":
        streams = [r for r in requests if r["kind"] == "mpegts"]
        if not any(_ok(r) for r in streams):
            return prefix + _refused(streams, "stream") + tail
        sent = sum(r.get("bytes", 0) for r in streams if _ok(r))
        return prefix + f"Home Assistant streamed {sent / 1_000_000:.1f} MB of video{over}." + tail
    return prefix + "No stream was requested." + tail


# --------------------------------------------------------------------------
# The report


def _host_kind(host: str) -> str:
    host = (host or "").split(":")[0].strip("[]").lower()
    if not host:
        return "unknown"
    if host in ("localhost", "127.0.0.1", "::1"):
        return "localhost"
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        return "IP address"
    if ":" in host:
        return "IPv6 address"
    if host.endswith(".local"):
        return ".local name"
    if host.endswith(".ui.nabu.casa"):
        return "Nabu Casa address"
    return "domain name"


def _clean(value: Any, limit: int = 160) -> str:
    """Text from the browser, made safe to print on one line."""
    text = str(value if value is not None else "")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:limit]


def _fmt_seconds(value: Any) -> str:
    try:
        return f"+{float(value):.1f}s".rjust(7)
    except (TypeError, ValueError):
        return "?".rjust(7)


def safe_code(value: Any) -> str:
    """A failure code from the browser, reduced to the characters codes use."""
    return re.sub(r"[^A-Za-z0-9-]", "", str(value or ""))[:12]


def _server_line(record: dict[str, Any]) -> str:
    status = record.get("status")
    parts = [record["kind"]]
    if record.get("name"):
        parts.append(record["name"])
    parts.append(str(status) if status is not None else "unfinished")
    extra = []
    if record.get("ms") is not None:
        extra.append(f"{record['ms']} ms")
    if record.get("bytes"):
        extra.append(f"{record['bytes']} B")
    if record.get("detail"):
        extra.append(record["detail"])
    line = f"  {_fmt_seconds(record['at'])}  {' '.join(parts)}"
    if extra:
        line += f"  ({', '.join(extra)})"
    return line


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(key) == b.get(key) for key in ("kind", "name", "status", "detail"))


def _server_lines(server: dict[str, Any] | None) -> list[str]:
    if not server or not server.get("requests"):
        return ["  (nothing)"]
    lines = []
    records = server["requests"]
    index = 0
    # A player reloading an unchanged playlist once a second would otherwise
    # bury everything else; a run of identical requests is one line.
    while index < len(records):
        run = index + 1
        while run < len(records) and _same(records[index], records[run]):
            run += 1
        lines.append(_server_line(records[index]))
        repeats = run - index - 1
        if repeats >= 2:
            lines.append(
                f"  {'':>7}  ... and {repeats} more like it, the last at "
                f"{_fmt_seconds(records[run - 1]['at']).strip()}"
            )
        elif repeats == 1:
            lines.append(_server_line(records[index + 1]))
        index = run
    if server.get("dropped"):
        lines.append(f"  ... {server['dropped']} more not recorded")
    return lines


def _client_lines(events: list[Any]) -> list[str]:
    if not events:
        return ["  (nothing)"]
    lines = []
    for event in events[:MAX_CLIENT_EVENTS]:
        if not isinstance(event, dict):
            continue
        name = _clean(event.get("e"), 40)
        state = []
        network = event.get("ns")
        ready = event.get("rs")
        if isinstance(network, int) and 0 <= network < len(NETWORK_STATES):
            state.append(f"network={NETWORK_STATES[network]}")
        if isinstance(ready, int) and 0 <= ready < len(READY_STATES):
            state.append(f"ready={READY_STATES[ready]}")
        if event.get("buf") is not None:
            state.append(f"buffered={_clean(event.get('buf'), 12)}s")
        if event.get("paused") is not None:
            # Not "playing": an element is unpaused from the moment play() is
            # called, long before any picture.
            state.append("paused" if event.get("paused") else "unpaused")
        if event.get("d"):
            state.append(_clean(event.get("d"), 80))
        line = f"  {_fmt_seconds(event.get('t'))}  {name}"
        if state:
            line += "  " + " ".join(state)
        lines.append(line)
    if len(events) > MAX_CLIENT_EVENTS:
        lines.append(f"  ... {len(events) - MAX_CLIENT_EVENTS} more not shown")
    return lines


def build_report(
    server: dict[str, Any] | None,
    client: dict[str, Any],
    *,
    slug: str,
    version: str,
    verdict_text: str | None = None,
) -> str:
    """The plain-text report the player copies to the clipboard."""
    client = client if isinstance(client, dict) else {}
    code = safe_code(client.get("code"))
    detail = _clean(client.get("detail"), 80)
    transport = {"hls": "HLS (native)", "mse": "MPEG-TS (mpegts.js)"}.get(
        client.get("transport") or "", "unknown"
    )
    protocol = _clean(client.get("protocol"), 8).rstrip(":") or "unknown"
    started = ""
    if server and server.get("started_wall"):
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(server["started_wall"]))

    lines = [
        "```",
        "Blink live view diagnostics",
        f"Verdict: {verdict_text or verdict(server, client)}",
        f"Code: {code or 'none'}" + (f" ({detail})" if detail else ""),
        f"Camera: {slug}   Path: {transport}   Integration: {version}",
        f"Page: {protocol}, {_host_kind(_clean(client.get('host'), 255))}",
        f"Device: {_clean(client.get('ua'), 200) or 'unknown'}",
    ]
    if started:
        lines.append(f"First request: {started}")
    if client.get("queued"):
        if client.get("unreachable"):
            lines.append("Sent late: the device could not reach Home Assistant when this failed.")
        else:
            answered = client.get("answered")
            status = f" (HTTP {int(answered)})" if isinstance(answered, int) and 100 <= answered < 600 else ""
            lines.append(f"Sent late: Home Assistant answered{status} but could not make the "
                         "report when this failed.")
    lines.append("")
    lines.append("Home Assistant received:")
    lines.extend(_server_lines(server))
    lines.append("")
    lines.append("The player saw:")
    lines.extend(_client_lines(client.get("events") or []))
    lines.append("```")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Saved reports
#
# The report used to exist only while the failure screen was open. Testing it
# in airplane mode showed why that is not enough: Home Assistant's own page
# reloaded the moment it reconnected, and the report went with it. Reports are
# now kept, per camera, for the admin panel to list and copy again later.

MAX_SAVED_REPORTS = 50


class ReportLog:
    """The most recent reports, newest last, at most one per session."""

    def __init__(self, reports: list[dict[str, Any]] | None = None) -> None:
        self.reports: list[dict[str, Any]] = [
            report for report in (reports or []) if isinstance(report, dict)
        ][-MAX_SAVED_REPORTS:]

    def add(self, report: dict[str, Any]) -> None:
        # A second report for the same session replaces the first: a failed
        # Save after a failed live view is one live view, not two.
        self.reports = [
            item for item in self.reports if item.get("session") != report.get("session")
        ]
        self.reports.append(report)
        self.reports = self.reports[-MAX_SAVED_REPORTS:]

    def clear(self, slug: str) -> int:
        before = len(self.reports)
        self.reports = [item for item in self.reports if item.get("slug") != slug]
        return before - len(self.reports)

    def sessions(self) -> set[str]:
        return {str(item.get("session")) for item in self.reports}


def history(
    log: ReportLog,
    trace: LiveviewTrace,
    *,
    slug: str | None,
    version: str,
) -> list[dict[str, Any]]:
    """Saved reports, plus recent live views nobody reported on, newest first."""
    entries = [
        {**item, "source": "device"}
        for item in log.reports
        if slug is None or item.get("slug") == slug
    ]
    reported = log.sessions()
    for session, server in trace.sessions():
        if session in reported or (slug is not None and server["slug"] != slug):
            continue
        summary = server_summary(server)
        entries.append({
            "id": f"live-{session[:12]}",
            "session": session,
            "slug": server["slug"],
            "at": server["started_wall"],
            "code": "",
            "verdict": summary,
            "queued": False,
            "source": "server",
            "report": build_report(
                server,
                {"transport": infer_transport(server)},
                slug=server["slug"],
                version=version,
                verdict_text=summary,
            ),
        })
    entries.sort(key=lambda item: float(item.get("at") or 0), reverse=True)
    return entries
