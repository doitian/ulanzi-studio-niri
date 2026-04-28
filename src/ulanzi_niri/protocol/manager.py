"""Device discovery and connection helpers."""

from __future__ import annotations

import logging
import time
from typing import Optional

import hid

from .ulanzi_d200x import UlanziD200XDevice

log = logging.getLogger(__name__)


def find_device_path() -> Optional[bytes]:
    """Return the hidraw path for the D200X control interface (interface 0)."""
    for entry in hid.enumerate(UlanziD200XDevice.USB_VENDOR_ID, UlanziD200XDevice.USB_PRODUCT_ID):
        if entry.get("interface_number") == UlanziD200XDevice.USB_INTERFACE:
            return entry["path"]
    return None


def open_device() -> Optional[UlanziD200XDevice]:
    """Open the first available D200X. Returns None if not connected."""
    path = find_device_path()
    if path is None:
        return None
    handle = hid.device()
    try:
        handle.open_path(path)
    except OSError as exc:
        log.warning("failed to open %r: %s (likely missing udev rule)", path, exc)
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
