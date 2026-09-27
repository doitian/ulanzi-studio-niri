"""Descriptor-based AU05 USB keepalive, without changing native input.

https://github.com/kubja/ulanzi-vibekey documents holding both HID interfaces
open to avoid the firmware's ~4-second idle reboot. No USB writes are needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .manager import _enumerate_hidraw

log = logging.getLogger(__name__)

USB_VENDOR_ID = 0xFFF1
USB_PRODUCT_ID = 0x00DD
INPUT_INTERFACE = 2
VENDOR_INTERFACE = 3


class UlanziAU05Device:
    def __init__(self, input_fd: int, vendor_fd: int) -> None:
        self._fds = [input_fd, vendor_fd]

    def close(self) -> None:
        fds, self._fds = self._fds, []
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass

    async def keep_alive(self) -> None:
        while self._fds:
            for fd in self._fds:
                # Drain raw copies to detect unplugging; kernel input is unaffected.
                for _ in range(32):
                    try:
                        data = os.read(fd, 128)
                    except BlockingIOError:
                        break
                    if not data:
                        raise OSError("AU05 HID endpoint closed")
            await asyncio.sleep(0.1)


def open_au05() -> UlanziAU05Device | None:
    """Open both interfaces of one AU05, never grabbing its input event devices."""
    devices: dict[Path, dict[int, Path]] = {}
    for info in _enumerate_hidraw(USB_VENDOR_ID, USB_PRODUCT_ID):
        hid = (info.sysfs / "device").resolve()
        devices.setdefault(hid.parent.parent, {})[info.interface_number] = info.dev_path
    for interfaces in devices.values():
        if INPUT_INTERFACE not in interfaces or VENDOR_INTERFACE not in interfaces:
            continue
        fds: list[int] = []
        try:
            for interface in (INPUT_INTERFACE, VENDOR_INTERFACE):
                fds.append(os.open(interfaces[interface], os.O_RDONLY | os.O_NONBLOCK))
            return UlanziAU05Device(fds[0], fds[1])
        except OSError as exc:
            log.warning("cannot open AU05 (check hidraw udev permissions): %s", exc)
            for fd in reversed(fds):
                os.close(fd)
    return None
