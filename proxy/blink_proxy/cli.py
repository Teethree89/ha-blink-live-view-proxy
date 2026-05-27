"""Command line interface for the Blink live-view proxy."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import signal
import time
from pathlib import Path

from aiohttp import web

from .blink import BlinkClient, BlinkStreamBroker, LiveViewHandle
from .clips import ClipManager, printable_clip
from .config import load_config, resolve_path
from .constants import LOGGER_NAME
from .routes import make_app

LOGGER = logging.getLogger(LOGGER_NAME)

async def command_list(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    client = BlinkClient(config, base, args.pin)
    try:
        await client.start()
        print(json.dumps({"cameras": client.list_cameras()}, indent=2, sort_keys=True))
    finally:
        await client.close()
    return 0

async def command_clips(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    client = BlinkClient(config, base, args.pin)
    try:
        await client.start()
        manager = ClipManager(client)
        clips = await manager.list_clips(
            source=args.source,
            camera_slug=args.camera,
            hours=args.hours,
            pages=args.pages,
            limit=args.limit,
        )
        print(
            json.dumps(
                {"count": len(clips), "clips": [printable_clip(clip) for clip in clips]},
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await client.close()
    return 0

async def command_save_clips(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    client = BlinkClient(config, base, args.pin)
    saved: list[str] = []
    try:
        await client.start()
        manager = ClipManager(client)
        clips = await manager.list_clips(
            source=args.source,
            camera_slug=args.camera,
            hours=args.hours,
            pages=args.pages,
            limit=args.limit,
        )
        if not clips:
            print(json.dumps({"saved": [], "message": "No matching clips found"}))
            return 0

        output_dir = resolve_path(args.output_dir, base)
        for clip in clips:
            path = await manager.save_clip(clip, output_dir)
            saved.append(str(path))
            LOGGER.info("Saved %s clip to %s", clip["source"], path)

        print(json.dumps({"saved": saved}, indent=2, sort_keys=True))
    finally:
        await client.close()
    return 0

async def command_probe(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    if args.liveview_token == "on":
        config["send_liveview_token"] = True
    elif args.liveview_token == "off":
        config["send_liveview_token"] = False
    client = BlinkClient(config, base, args.pin)
    bytes_seen = 0
    packets_seen = 0
    read_timeouts = 0
    liveview: LiveViewHandle | None = None
    writer: asyncio.StreamWriter | None = None

    try:
        await client.start()
        broker = BlinkStreamBroker(client)
        liveview = await broker.start_liveview(args.slug)
        reader, writer = await asyncio.open_connection(liveview.host, liveview.port)
        deadline = time.monotonic() + args.seconds
        output = open(args.output, "wb") if args.output else None
        try:
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                timeout = min(args.read_timeout, remaining)
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(188 * 64), timeout=timeout
                    )
                except TimeoutError:
                    read_timeouts += 1
                    LOGGER.debug(
                        "Still waiting for MPEG-TS bytes from %s (%d timeout%s)",
                        args.slug,
                        read_timeouts,
                        "" if read_timeouts == 1 else "s",
                    )
                    continue
                if not chunk:
                    break
                bytes_seen += len(chunk)
                packets_seen += sum(
                    1 for offset in range(0, len(chunk), 188) if chunk[offset] == 0x47
                )
                if output:
                    output.write(chunk)
        finally:
            if output:
                output.close()
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if liveview is not None:
            await liveview.close()
        await client.close()

    print(
        json.dumps(
            {
                "slug": args.slug,
                "seconds": args.seconds,
                "bytes": bytes_seen,
                "mpegts_sync_packets": packets_seen,
                "read_timeouts": read_timeouts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0

async def command_serve(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port

    app = await make_app(config, base, args.pin)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config["host"], int(config["port"]))
    await site.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    LOGGER.info("Serving on http://%s:%s", config["host"], config["port"])
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to JSON config; defaults to BLINK_PROXY_CONFIG or config.json "
            "next to this script"
        ),
    )
    parser.add_argument("--pin", help="Blink 2FA code for first login")
    parser.add_argument("--log-level", default="INFO")

    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the HTTP proxy")
    serve.add_argument("--host", help="Override listen host")
    serve.add_argument("--port", type=int, help="Override listen port")
    serve.set_defaults(func=command_serve)

    list_cmd = subparsers.add_parser("list", help="List discovered Blink cameras")
    list_cmd.set_defaults(func=command_list)

    clips = subparsers.add_parser("clips", help="List recent cloud/local clips")
    clips.add_argument("--camera", help="Optional camera slug, for example driveway")
    clips.add_argument(
        "--source",
        choices=("both", "cloud", "local"),
        default="both",
        help="Clip source to inspect",
    )
    clips.add_argument("--hours", type=float, default=24)
    clips.add_argument("--pages", type=int, default=3, help="Cloud clip pages to scan")
    clips.add_argument("--limit", type=int, default=20)
    clips.set_defaults(func=command_clips)

    save_clips = subparsers.add_parser("save-clips", help="Save recent clips")
    save_clips.add_argument(
        "--camera", help="Optional camera slug, for example driveway"
    )
    save_clips.add_argument(
        "--source",
        choices=("both", "cloud", "local"),
        default="both",
        help="Clip source to inspect",
    )
    save_clips.add_argument("--hours", type=float, default=24)
    save_clips.add_argument(
        "--pages", type=int, default=3, help="Cloud clip pages to scan"
    )
    save_clips.add_argument("--limit", type=int, default=1)
    save_clips.add_argument("--output-dir", default="clips")
    save_clips.set_defaults(func=command_save_clips)

    probe = subparsers.add_parser("probe", help="Read live MPEG-TS bytes briefly")
    probe.add_argument("slug", help="Camera slug from config or list output")
    probe.add_argument("--seconds", type=float, default=10)
    probe.add_argument("--read-timeout", type=float, default=5)
    probe.add_argument("--output", help="Optional .ts capture path for ffprobe testing")
    probe.add_argument(
        "--liveview-token",
        choices=("config", "on", "off"),
        default="config",
        help="Override whether the IMMI auth header includes liveview_token",
    )
    probe.set_defaults(func=command_probe)

    return parser

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(args.func(args))
