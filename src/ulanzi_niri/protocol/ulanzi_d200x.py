"""Ulanzi Stream Controller D200X protocol implementation.

Wire format (adapted from redphx/strmdck's D200 work):

    7c 7c | cmd:u16 BE | length:u32 LE | data[1016]   = 1024-byte packet

Outgoing commands recognized by the firmware:

    0x0001 SET_BUTTONS               - full button manifest (zip stream)
    0x0002 ENABLE_INPUT_STREAMING    - empty payload; unlocks IN_BUTTON
                                       events for the 4th-row hardware
                                       buttons (pos 14/15) and the rotary
                                       encoders. Without this the firmware
                                       silently routes encoder rotates to
                                       its built-in brightness handler and
                                       eats the HW button presses entirely.
                                       Discovered by opcode sweep, 2026-04-28.
    0x000d PARTIALLY_UPDATE_BUTTONS  - incremental manifest
    0x0006 SET_SMALL_WINDOW_DATA     - wide-tile content (clock/stats/etc)
    0x000a SET_BRIGHTNESS            - 0..100 as ASCII
    0x000b SET_LABEL_STYLE           - JSON style for button labels
    0x0003 GET_DEVICE_INFO           - request device-info JSON banner

Incoming:

    0x0101 BUTTON                    - press/release events; see _parse_button
                                       for the wire-index -> config-pos map.
                                       4th-row HW buttons (pos 14/15) and
                                       encoders are only emitted after the
                                       host sends ENABLE_INPUT_STREAMING
                                       (0x0002).
    0x0303 DEVICE_INFO                - JSON banner with serial, version, etcThe D200X grid is:

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
    OUT_ENABLE_INPUT_STREAMING = 0x0002
    OUT_GET_DEVICE_INFO = 0x0003
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

# Wire indices in the IN_BUTTON payload (state | index | marker | value | ...).
#   marker 0x01: physical buttons. Wire indices 0..12 -> LCD pos 0..12,
#                wire index 13 -> wide tile (pos 13). Wire indices 14 are
#                reserved/unused; wire index 15 -> hardware button left
#                (config pos 14), wire index 16 -> hardware button right
#                (config pos 15).
#   marker 0x02: encoders. Wire indices 17..19 -> encoder 0..2.
#                value 0x00/0x01 = release/press click; value 0x02 = rotate
#                clockwise (single click pulse); value 0x03 = rotate
#                counter-clockwise. Encoders never emit a release for rotate.
#
# Confirmed via Windows USB sniff of the official Ulanzi app on 2026-04-28.
WIRE_INDEX_LCD_MAX = 12
WIRE_INDEX_WIDE_TILE = 13
WIRE_INDEX_EXTRA_BUTTON_LEFT = 15
WIRE_INDEX_EXTRA_BUTTON_RIGHT = 16
WIRE_INDEX_ENCODER_BASE = 17  # 17, 18, 19

ENCODER_VALUE_RELEASE = 0x00
ENCODER_VALUE_PRESS = 0x01
ENCODER_VALUE_ROTATE_CCW = 0x02
ENCODER_VALUE_ROTATE_CW = 0x03

WIDE_TILE_POS = 13
EXTRA_BUTTON_POS_BASE = 14   # config pos 14, 15
ENCODER_INDEX_MAX = 2        # config encoder.index 0..2

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

    async def request_device_info(self) -> None:
        """Ask the firmware to emit IN_DEVICE_INFO (banner JSON).

        Triggers an IN_DEVICE_INFO reply with serial / firmware version /
        hardware revision, useful for logging on connect. Does NOT enable
        input streaming on its own; use enable_input_streaming for that.
        """
        packet = build_packet(CommandProtocol.OUT_GET_DEVICE_INFO, b"")
        await self.write_packet(packet)

    async def enable_input_streaming(self) -> None:
        """Unlock IN_BUTTON events for the 4th-row HW buttons + encoders.

        Without this opcode the firmware silently consumes encoder rotates
        (routing them to its built-in brightness handler) and drops 4th-row
        button presses entirely. Send once after every connect / reconnect.

        Discovered empirically by opcode sweep on 2026-04-28; the official
        Windows app sends the same packet shortly after handshake. Empty
        payload; no reply expected.
        """
        packet = build_packet(CommandProtocol.OUT_ENABLE_INPUT_STREAMING, b"")
        await self.write_packet(packet)

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

    # Button payload layout:
    #   state:u8 | index:u8 | marker:u8 | value:u8 | ...
    #
    # marker 0x01: physical button (LCD 0..12, wide tile 13, hw buttons at
    # wire indices 15/16 mapped to config pos 14/15). value = 1 press, 0 release.
    #
    # marker 0x02: encoder. wire indices 17..19 = encoder 0..2.
    # value 0/1 = release/press click; value 2/3 = rotate CW/CCW (one pulse
    # per packet, no release).
    def _parse_button(self, body: bytes, *, raw: bytes) -> list[DeckEvent]:
        if len(body) < 4:
            return [DeckEvent(kind=DeckEventKind.UNKNOWN, raw=raw)]
        state, index, marker, value = body[0], body[1], body[2], body[3]

        if marker == 0x01:
            if index <= WIRE_INDEX_WIDE_TILE:
                # LCD pos 0..12 + wide tile (pos 13)
                return [
                    DeckEvent(
                        kind=DeckEventKind.LCD_BUTTON,
                        pos=index,
                        pressed=bool(value),
                        raw=raw,
                        extras={"state": state},
                    )
                ]
            if index == WIRE_INDEX_EXTRA_BUTTON_LEFT:
                return [
                    DeckEvent(
                        kind=DeckEventKind.EXTRA_BUTTON,
                        pos=EXTRA_BUTTON_POS_BASE,  # 14
                        pressed=bool(value),
                        raw=raw,
                        extras={"state": state},
                    )
                ]
            if index == WIRE_INDEX_EXTRA_BUTTON_RIGHT:
                return [
                    DeckEvent(
                        kind=DeckEventKind.EXTRA_BUTTON,
                        pos=EXTRA_BUTTON_POS_BASE + 1,  # 15
                        pressed=bool(value),
                        raw=raw,
                        extras={"state": state},
                    )
                ]

        if marker == 0x02:
            enc_index = index - WIRE_INDEX_ENCODER_BASE
            if 0 <= enc_index <= ENCODER_INDEX_MAX:
                if value in (ENCODER_VALUE_PRESS, ENCODER_VALUE_RELEASE):
                    return [
                        DeckEvent(
                            kind=DeckEventKind.ENCODER_PRESS,
                            encoder_index=enc_index,
                            pressed=value == ENCODER_VALUE_PRESS,
                            raw=raw,
                            extras={"state": state},
                        )
                    ]
                if value == ENCODER_VALUE_ROTATE_CW:
                    return [
                        DeckEvent(
                            kind=DeckEventKind.ENCODER_ROTATE,
                            encoder_index=enc_index,
                            delta=1,
                            raw=raw,
                            extras={"state": state},
                        )
                    ]
                if value == ENCODER_VALUE_ROTATE_CCW:
                    return [
                        DeckEvent(
                            kind=DeckEventKind.ENCODER_ROTATE,
                            encoder_index=enc_index,
                            delta=-1,
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
