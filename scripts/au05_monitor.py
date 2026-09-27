"""Capture AU05 reports while keeping both interfaces open; no actions or USB writes."""

from __future__ import annotations

import argparse
import asyncio
import logging

from ulanzi_niri.protocol.ulanzi_au05 import open_au05


async def monitor(seconds: float) -> int:
    device = open_au05(grab_input=False)
    if device is None:
        print("AU05 not found or inaccessible; install the udev rules and replug it.")
        return 1
    print("Keepalive active. Press/release Voice, Enter, Esc, wheel click, then turn both ways.")
    print("Native key/scroll input is NOT suppressed. Leave focus on this terminal.")
    try:
        async with asyncio.timeout(seconds):
            async for event in device:
                print(event, flush=True)
    except TimeoutError:
        print(f"Capture completed after {seconds:g}s without a HID read error.")
    except OSError as exc:
        print(f"Device disconnected: {exc}")
        return 1
    finally:
        device.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    if not 0 < args.seconds < float("inf"):
        parser.error("--seconds must be a finite positive number")
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(message)s")
    try:
        return asyncio.run(monitor(args.seconds))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
