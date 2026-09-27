"""AU05 raw HID input and descriptor-based USB keepalive.

Protocol reference: https://github.com/kubja/ulanzi-vibekey
Both interfaces must stay open to avoid the firmware's ~4-second idle reboot.
No vendor writes or synthetic heartbeat packets are needed.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .manager import _enumerate_hidraw

log = logging.getLogger(__name__)

USB_VENDOR_ID = 0xFFF1
USB_PRODUCT_ID = 0x00DD
INPUT_INTERFACE = 2
VENDOR_INTERFACE = 3
EVIOCGRAB = 0x40044590


@dataclass(frozen=True)
class AU05Event:
    report: Literal["keyboard", "consumer", "mouse", "wheel"]
    code: int = 0
    pressed: bool = False
    delta: int = 0


class AU05Decoder:
    def __init__(self) -> None:
        self._held: dict[str, set[int]] = {}

    def decode(self, data: bytes) -> list[AU05Event]:
        events: list[AU05Event] = []
        report: Literal["keyboard", "consumer", "mouse"]
        if data[:1] == b"\x03" and len(data) >= 9:
            report = "keyboard"
            # 0x01 is the AU05 Voice key, not an ErrorRollOver report.
            held = {code for code in data[3:9] if code}
            held.update(0xE0 + bit for bit in range(8) if data[1] & (1 << bit))
        elif data[:1] == b"\x01" and len(data) >= 3:
            report = "consumer"
            code = int.from_bytes(data[1:3], "little")
            held = {code} if code else set()
        elif data[:1] == b"\x02" and len(data) >= 6:
            report = "mouse"
            held = {bit + 1 for bit in range(8) if data[1] & (1 << bit)}
            delta = int.from_bytes(data[4:5], "little", signed=True)
            if delta:
                events.append(AU05Event("wheel", delta=delta))
        else:
            return events
        previous = self._held.get(report, set())
        events.extend(AU05Event(report, code, False) for code in sorted(previous - held))
        events.extend(AU05Event(report, code, True) for code in sorted(held - previous))
        self._held[report] = held
        return events


class UlanziAU05Device:
    def __init__(self, input_fd: int, vendor_fd: int, grabbed_fds: list[int]) -> None:
        self._fds = [input_fd, vendor_fd, *grabbed_fds]
        self._decoder = AU05Decoder()

    def close(self) -> None:
        fds, self._fds = self._fds, []
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass

    async def __aiter__(self) -> AsyncIterator[AU05Event]:
        while self._fds:
            for index, fd in enumerate(self._fds[:2]):
                # Bound draining so a noisy endpoint cannot starve shutdown/reload.
                for _ in range(32):
                    try:
                        data = os.read(fd, 128)
                    except BlockingIOError:
                        break
                    if not data:
                        raise OSError("AU05 HID endpoint closed")
                    if index == 0:
                        log.debug("AU05 input: %s", data.hex(" "))
                        for event in self._decoder.decode(data):
                            yield event
            await asyncio.sleep(0.01)


def open_au05(*, grab_input: bool = True) -> UlanziAU05Device | None:
    """Open a complete interface pair belonging to the same physical USB device."""
    devices: dict[Path, dict[int, tuple[Path, Path]]] = {}
    for info in _enumerate_hidraw(USB_VENDOR_ID, USB_PRODUCT_ID):
        hid = (info.sysfs / "device").resolve()
        devices.setdefault(hid.parent.parent, {})[info.interface_number] = (info.dev_path, hid)
    for interfaces in devices.values():
        if INPUT_INTERFACE not in interfaces or VENDOR_INTERFACE not in interfaces:
            continue
        fds: list[int] = []
        try:
            for interface in (INPUT_INTERFACE, VENDOR_INTERFACE):
                fds.append(os.open(interfaces[interface][0], os.O_RDONLY | os.O_NONBLOCK))
            if grab_input:
                # hidraw alone does not suppress the kernel's Enter/Esc/scroll events.
                hid = interfaces[INPUT_INTERFACE][1]
                events = sorted(hid.glob("input/input*/event*"))
                if not events:
                    raise OSError("AU05 input event nodes not ready")
                for event in events:
                    fd = os.open(Path("/dev/input") / event.name, os.O_RDONLY | os.O_NONBLOCK)
                    fds.append(fd)
                    fcntl.ioctl(fd, EVIOCGRAB, 1)
            return UlanziAU05Device(fds[0], fds[1], fds[2:])
        except OSError as exc:
            log.warning("cannot open AU05 (check udev permissions/input grabs): %s", exc)
            for fd in reversed(fds):
                os.close(fd)
    return None
