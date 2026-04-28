"""Base abstractions for HID-based stream-deck-like devices.

The protocol implementation is loosely modeled on redphx/strmdck's D200 work
but specialized for the Ulanzi D200X (one wide LCD, two plain buttons, three
rotary encoders).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class DeckEventKind(Enum):
    LCD_BUTTON = "lcd_button"
    EXTRA_BUTTON = "extra_button"
    ENCODER_PRESS = "encoder_press"
    ENCODER_ROTATE = "encoder_rotate"
    DEVICE_INFO = "device_info"
    UNKNOWN = "unknown"


@dataclass
class DeckEvent:
    kind: DeckEventKind
    pos: int | None = None
    encoder_index: int | None = None
    pressed: bool | None = None
    delta: int | None = None
    raw: bytes | None = None
    info: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


class DeckDevice(ABC):
    """Abstract base for a HID-attached deck device."""

    POLLING_INTERVAL = 0.01
    READ_BUFFER = 1024

    def __init__(self, hid_device: Any) -> None:
        self._hid = hid_device
        self._write_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._hid.close()
        except Exception:  # noqa: BLE001
            log.debug("hid close raised", exc_info=True)

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ IO
    async def read_events(self) -> Iterable[DeckEvent]:
        """Async generator-like coroutine that reads one batch of events."""
        raise NotImplementedError  # use async iterator interface instead

    def __aiter__(self) -> DeckDevice:
        return self

    async def __anext__(self) -> DeckEvent:
        while True:
            if self._closed:
                raise StopAsyncIteration
            try:
                data = self._hid.read(self.READ_BUFFER)
            except OSError as exc:
                log.warning("hid read failed: %s", exc)
                self.close()
                raise StopAsyncIteration from exc
            if not data:
                await asyncio.sleep(self.POLLING_INTERVAL)
                continue
            for event in self._parse_input(bytes(data)):
                return event
            await asyncio.sleep(0)

    async def write_packet(self, packet: bytes | list[bytes]) -> None:
        async with self._write_lock:
            if self._closed:
                return
            try:
                if isinstance(packet, list):
                    for chunk in packet:
                        self._hid.write(chunk)
                else:
                    self._hid.write(packet)
            except OSError as exc:
                log.warning("hid write failed: %s", exc)
                self.close()

    # ------------------------------------------------------------------ abstract
    @abstractmethod
    def _parse_input(self, data: bytes) -> list[DeckEvent]:
        ...
