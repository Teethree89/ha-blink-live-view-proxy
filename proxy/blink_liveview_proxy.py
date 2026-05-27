#!/usr/bin/env python3
"""Compatibility entrypoint for the modular Blink live-view proxy."""

from blink_proxy.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
