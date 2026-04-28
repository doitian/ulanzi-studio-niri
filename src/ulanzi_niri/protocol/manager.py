"""Discover and open the D200X via raw /dev/hidraw devices.

We bypass libhidapi entirely: the PyPI hidapi wheel ships with a libusb
backend that conflicts with the kernel's usbhid driver. Reading/writing
/dev/hidrawN directly works around this and removes a native dependency.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .ulanzi_d200x import UlanziD200XDevice

log = logging.getLogger(__name__)


_SYSFS_HIDRAW = Path("/sys/class/hidraw")


@dataclass(frozen=True)
class HidrawInfo:
    dev_path: Path  # /dev/hidrawN
    sysfs: Path  # /sys/class/hidraw/hidrawN
    vendor_id: int
    product_id: int
    interface_number: int


def _read_hex(path: Path) -> int | None:
    try:
        return int(path.read_text().strip(), 16)
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _enumerate_hidraw(vid: int, pid: int) -> list[HidrawInfo]:
    """Walk /sys/class/hidraw/* and return entries matching vid:pid."""
    out: list[HidrawInfo] = []
    if not _SYSFS_HIDRAW.is_dir():
        return out
    for entry in sorted(_SYSFS_HIDRAW.iterdir()):
        # hidrawN -> .../usbN/.../hidraw/hidrawN
        device_link = entry / "device"
        try:
            hid_dev = device_link.resolve()
        except OSError:
            continue
        # hid_dev looks like /sys/.../<usbX-Y.Z>:1.0/0003:VID:PID.001C
        # Walk up to find idVendor/idProduct (the USB device node) and the interface number.
        intf_dir = hid_dev.parent  # the :1.X directory
        usb_dev = intf_dir.parent
        vendor = _read_hex(usb_dev / "idVendor")
        product = _read_hex(usb_dev / "idProduct")
        if vendor != vid or product != pid:
            continue
        intf_num = _read_int(intf_dir / "bInterfaceNumber")
        out.append(
            HidrawInfo(
                dev_path=Path("/dev") / entry.name,
                sysfs=entry,
                vendor_id=vendor,
                product_id=product,
                interface_number=intf_num if intf_num is not None else -1,
            )
        )
    return out


class HidrawHandle:
    """Minimal /dev/hidrawN file handle exposing the API DeckDevice expects.

    We expose `read(size)` and `write(data)` matching cython-hidapi semantics
    (non-blocking read returns b'' if no data; write returns the number of
    bytes written). The `set_nonblocking` toggle adjusts `O_NONBLOCK`.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd = os.open(str(path), os.O_RDWR)
        self._nonblocking = False

    def set_nonblocking(self, on: bool) -> None:
        import fcntl

        cur = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        if on:
            fcntl.fcntl(self._fd, fcntl.F_SETFL, cur | os.O_NONBLOCK)
        else:
            fcntl.fcntl(self._fd, fcntl.F_SETFL, cur & ~os.O_NONBLOCK)
        self._nonblocking = on

    def read(self, size: int) -> bytes:
        try:
            data = os.read(self._fd, size)
        except BlockingIOError:
            return b""
        return data

    def write(self, data: bytes) -> int:
        return os.write(self._fd, data)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


def find_device_path() -> Path | None:
    """Return the /dev/hidrawN path for the D200X control interface (interface 0)."""
    for info in _enumerate_hidraw(
        UlanziD200XDevice.USB_VENDOR_ID, UlanziD200XDevice.USB_PRODUCT_ID
    ):
        if info.interface_number == UlanziD200XDevice.USB_INTERFACE:
            return info.dev_path
    return None


def open_device() -> UlanziD200XDevice | None:
    """Open the D200X control interface; returns None if unavailable."""
    path = find_device_path()
    if path is None:
        return None
    try:
        handle = HidrawHandle(path)
    except OSError as exc:
        log.warning("failed to open %s: %s (check udev rule and group membership)", path, exc)
        return None
    handle.set_nonblocking(True)
    return UlanziD200XDevice(handle)


def wait_for_device(poll_interval: float = 2.0) -> UlanziD200XDevice:
    """Block until the device is plugged in and openable, then return it."""
    while True:
        dev = open_device()
        if dev is not None:
            return dev
        time.sleep(poll_interval)
