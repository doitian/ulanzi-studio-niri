"""Ulanzi Stream Controller D200X protocol implementation.

Wire format (adapted from redphx/strmdck's D200 work):

    7c 7c | cmd:u16 BE | length:u32 LE | data[1016]   = 1024-byte packet

Outgoing commands recognized by the firmware:

    0x0001 SET_BUTTONS               - full button manifest (zip stream)
    0x000d PARTIALLY_UPDATE_BUTTONS  - incremental manifest
    0x0006 SET_SMALL_WINDOW_DATA     - wide-tile content (clock/stats/etc)
    0x000a SET_BRIGHTNESS            - 0..100 as ASCII
    0x000b SET_LABEL_STYLE           - JSON style for button labels

Incoming:

    0x0101 BUTTON                    - press/release events
    0x0303 DEVICE_INFO               - banner string

The D200X grid is:

    Row 0 (LCD): pos 0..4   (5 x 196x196)
    Row 1 (LCD): pos 5..9   (5 x 196x196)
    Row 2 (LCD): pos 10..12 (3 x 196x196) + wide tile (458x196)
    Row 3 (HW):  pos 14, 15 (plain buttons) + 3 rotary encoders

The wide tile is owned by the small-window subsystem and is not part of the
manifest-managed button set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from construct import Bytes, BytesInteger, ByteSwapped, Const, GreedyBytes, Int32ub, Padded, Struct

from .device import DeckDevice, DeckEvent, DeckEventKind

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- protocol constants
class CommandProtocol(IntEnum):
    OUT_SET_BUTTONS = 0x0001
    OUT_PARTIALLY_UPDATE_BUTTONS = 0x000D
    OUT_SET_SMALL_WINDOW_DATA = 0x0006
    OUT_SET_BRIGHTNESS = 0x000A
    OUT_SET_LABEL_STYLE = 0x000B

    IN_BUTTON = 0x0101
    IN_DEVICE_INFO = 0x0303


PACKET_SIZE = 1024
HEADER_SIZE = 8
PAYLOAD_SIZE = PACKET_SIZE - HEADER_SIZE  # 1016
MAGIC = b"\x7c\x7c"

PacketStruct = Struct(
    "magic" / Const(MAGIC),
    "command_protocol" / BytesInteger(2),
    "length" / ByteSwapped(Int32ub),
    "data" / Padded(PAYLOAD_SIZE, GreedyBytes),
)

IncomingHeader = Struct(
    "magic" / Bytes(2),
    "command_protocol" / BytesInteger(2),
    "length" / ByteSwapped(Int32ub),
)


def build_packet(command: CommandProtocol, payload: bytes, length: int | None = None) -> bytes:
    """Build a single 1024-byte HID OUT packet."""
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError(
            f"payload chunk {len(payload)} exceeds {PAYLOAD_SIZE}; chunk it before calling build_packet"
        )
    return PacketStruct.build(
        dict(
            command_protocol=int(command),
            length=length if length is not None else len(payload),
            data=payload.ljust(PAYLOAD_SIZE, b"\x00"),
        )
    )


def chunk_for_zip(command: CommandProtocol, blob: bytes) -> list[bytes]:
    """Build the multi-packet sequence used to upload a button-manifest ZIP.

    The first packet carries the header + first 1016 bytes of the ZIP and the
    total length; subsequent packets are raw 1024-byte slices of the ZIP
    (no header), padded with zeros if needed.
    """
    file_size = len(blob)
    head = blob[:PAYLOAD_SIZE]
    packets = [build_packet(command, head, length=file_size)]
    for offset in range(PAYLOAD_SIZE, file_size, PACKET_SIZE):
        chunk = blob[offset : offset + PACKET_SIZE]
        if len(chunk) < PACKET_SIZE:
            chunk = chunk.ljust(PACKET_SIZE, b"\x00")
        packets.append(chunk)
    return packets


# ---------------------------------------------------------------------------- geometry
@dataclass(frozen=True)
class TileGeometry:
    col: int
    row: int
    width: int
    height: int


LCD_BUTTON_COUNT = 13
EXTRA_BUTTON_COUNT = 2
ENCODER_COUNT = 3

WIDE_TILE_POS = 13
EXTRA_BUTTON_POS_BASE = 14   # 14, 15
ENCODER_PRESS_POS_BASE = 16  # 16, 17, 18

STD_ICON = (196, 196)
WIDE_ICON = (458, 196)

BUTTON_GEOMETRY: dict[int, TileGeometry] = {
    # Row 0
    0: TileGeometry(0, 0, *STD_ICON),
    1: TileGeometry(1, 0, *STD_ICON),
    2: TileGeometry(2, 0, *STD_ICON),
    3: TileGeometry(3, 0, *STD_ICON),
    4: TileGeometry(4, 0, *STD_ICON),
    # Row 1
    5: TileGeometry(0, 1, *STD_ICON),
    6: TileGeometry(1, 1, *STD_ICON),
    7: TileGeometry(2, 1, *STD_ICON),
    8: TileGeometry(3, 1, *STD_ICON),
    9: TileGeometry(4, 1, *STD_ICON),
    # Row 2 (LCD region; pos 13 is wide and managed via small-window opcode)
    10: TileGeometry(0, 2, *STD_ICON),
    11: TileGeometry(1, 2, *STD_ICON),
    12: TileGeometry(2, 2, *STD_ICON),
}

WIDE_TILE_GEOMETRY = TileGeometry(3, 2, *WIDE_ICON)


# ---------------------------------------------------------------------------- wide-tile modes
class SmallWindowMode(IntEnum):
    STATS = 0
    CLOCK = 1
    BACKGROUND = 2
    ENCODERS = 3  # experimental on D200X


# ---------------------------------------------------------------------------- device class
class UlanziD200XDevice(DeckDevice):
    USB_VENDOR_ID = 0x2207
    USB_PRODUCT_ID = 0x0019
    USB_INTERFACE = 0

    LCD_BUTTON_COUNT = LCD_BUTTON_COUNT
    EXTRA_BUTTON_COUNT = EXTRA_BUTTON_COUNT
    ENCODER_COUNT = ENCODER_COUNT
    DECK_NAME = "Ulanzi Stream Controller D200X"

    def __init__(self, hid_device: Any) -> None:
        super().__init__(hid_device)
        self._brightness: int | None = None
        self._label_style: dict | None = None
        self._last_small_window: str | None = None

    # ------------------------------------------------------------------ outgoing
    async def set_brightness(self, brightness: int, *, force: bool = False) -> None:
        brightness = max(0, min(100, int(brightness)))
        if not force and brightness == self._brightness:
            return
        self._brightness = brightness
        packet = build_packet(
            CommandProtocol.OUT_SET_BRIGHTNESS, str(brightness).encode("ascii")
        )
        await self.write_packet(packet)

    async def set_label_style(self, style: dict, *, force: bool = False) -> None:
        if not force and style == self._label_style:
            return
        self._label_style = dict(style)
        import json

        wire = {
            "Align": style.get("align", "bottom"),
            "Color": int(style.get("color", "FFFFFF"), 16),
            "FontName": style.get("font_name", "Roboto"),
            "ShowTitle": bool(style.get("show_title", True)),
            "Size": int(style.get("size", 10)),
            "Weight": int(style.get("weight", 80)),
        }
        packet = build_packet(
            CommandProtocol.OUT_SET_LABEL_STYLE,
            json.dumps(wire, separators=(",", ":")).encode("utf-8"),
        )
        await self.write_packet(packet)

    async def set_small_window(self, payload: str, *, force: bool = False) -> None:
        """Send a raw small-window payload (mode|...).

        Callers in wide_tile.py compose the payload; this method just frames it.
        """
        if not force and payload == self._last_small_window:
            return
        self._last_small_window = payload
        packet = build_packet(
            CommandProtocol.OUT_SET_SMALL_WINDOW_DATA, payload.encode("utf-8")
        )
        await self.write_packet(packet)

    async def push_buttons_zip(self, blob: bytes, *, update_only: bool = False) -> None:
        cmd = (
            CommandProtocol.OUT_PARTIALLY_UPDATE_BUTTONS
            if update_only
            else CommandProtocol.OUT_SET_BUTTONS
        )
        packets = chunk_for_zip(cmd, blob)
        await self.write_packet(packets)

    # ------------------------------------------------------------------ incoming
    def _parse_input(self, data: bytes) -> list[DeckEvent]:
        if len(data) < HEADER_SIZE:
            return [DeckEvent(kind=DeckEventKind.UNKNOWN, raw=data)]
        if data[:2] != MAGIC:
            return [DeckEvent(kind=DeckEventKind.UNKNOWN, raw=data)]

        try:
            header = IncomingHeader.parse(data[:HEADER_SIZE])
        except Exception:  # noqa: BLE001
            return [DeckEvent(kind=DeckEventKind.UNKNOWN, raw=data)]

        cmd = header["command_protocol"]
        length = header["length"]
        body = data[HEADER_SIZE : HEADER_SIZE + max(0, length)]

        if cmd == CommandProtocol.IN_DEVICE_INFO:
            text = body.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            return [DeckEvent(kind=DeckEventKind.DEVICE_INFO, info=text, raw=data)]

        if cmd == CommandProtocol.IN_BUTTON:
            return self._parse_button(body, raw=data)

        return [DeckEvent(kind=DeckEventKind.UNKNOWN, raw=data, extras={"cmd": cmd})]

    # Button payload layout (confirmed for LCD + wide tile):
    #   state:u8 | index:u8 | const 0x01 | pressed:u8 | ...
    #
    # v0 limitation: the firmware does not stream events for the two extra
    # hardware buttons (pos 14, 15) or the three rotary encoders (rotate or
    # press) on interface 0 after a manifest is pushed; they also do not
    # appear on the boot-keyboard interface 1. Reverse-engineering of the
    # opcode required to enable that streaming is a TODO. Any unexpected
    # frame is surfaced as UNKNOWN with the raw bytes preserved.
    def _parse_button(self, body: bytes, *, raw: bytes) -> list[DeckEvent]:
        if len(body) < 4:
            return [DeckEvent(kind=DeckEventKind.UNKNOWN, raw=raw)]
        state, index, marker, value = body[0], body[1], body[2], body[3]

        # LCD + wide-tile press/release: index 0..13 -> pos 0..13
        if marker == 0x01 and index <= WIDE_TILE_POS:
            return [
                DeckEvent(
                    kind=DeckEventKind.LCD_BUTTON,
                    pos=index,
                    pressed=bool(value),
                    raw=raw,
                    extras={"state": state},
                )
            ]

        return [
            DeckEvent(
                kind=DeckEventKind.UNKNOWN,
                raw=raw,
                extras={"state": state, "index": index, "marker": marker, "value": value},
            )
        ]
