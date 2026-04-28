"""Smoke tests for protocol packet framing."""

from __future__ import annotations

from ulanzi_niri.protocol.device import DeckEventKind
from ulanzi_niri.protocol.ulanzi_d200x import (
    HEADER_SIZE,
    MAGIC,
    PACKET_SIZE,
    PAYLOAD_SIZE,
    CommandProtocol,
    UlanziD200XDevice,
    build_packet,
    chunk_for_zip,
)


def test_brightness_packet_layout() -> None:
    pkt = build_packet(CommandProtocol.OUT_SET_BRIGHTNESS, b"70")
    assert len(pkt) == PACKET_SIZE
    assert pkt[:2] == MAGIC
    # cmd (BE u16) = 0x000a
    assert pkt[2:4] == b"\x00\x0a"
    # length (LE u32) = 2
    assert pkt[4:8] == b"\x02\x00\x00\x00"
    assert pkt[8:10] == b"70"
    assert pkt[10:HEADER_SIZE + 4] == b"\x00\x00"


def test_chunk_for_zip_first_packet_carries_total_length() -> None:
    blob = b"X" * (PAYLOAD_SIZE + 100)
    pkts = chunk_for_zip(CommandProtocol.OUT_SET_BUTTONS, blob)
    assert len(pkts) == 2
    assert all(len(p) == PACKET_SIZE for p in pkts)
    # First packet: length field = total file size
    length_le = pkts[0][4:8]
    assert int.from_bytes(length_le, "little") == len(blob)


def test_payload_chunk_overflow_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_packet(CommandProtocol.OUT_SET_BRIGHTNESS, b"X" * (PAYLOAD_SIZE + 1))


def _button_packet(state: int, index: int, marker: int, value: int) -> bytes:
    body = bytes([state, index, marker, value])
    # IN_BUTTON cmd = 0x0101; build incoming-style packet manually since
    # build_packet uses BE for cmd which matches.
    pkt = build_packet(CommandProtocol.IN_BUTTON, body)
    assert pkt[:2] == MAGIC
    return pkt


def _device() -> UlanziD200XDevice:
    return UlanziD200XDevice(None)  # type: ignore[arg-type]


def test_parse_lcd_button_press_release() -> None:
    dev = _device()
    [press] = dev._parse_input(_button_packet(0x01, 0x05, 0x01, 0x01))
    assert press.kind == DeckEventKind.LCD_BUTTON
    assert press.pos == 5
    assert press.pressed is True

    [release] = dev._parse_input(_button_packet(0x01, 0x05, 0x01, 0x00))
    assert release.kind == DeckEventKind.LCD_BUTTON
    assert release.pressed is False


def test_parse_wide_tile_press() -> None:
    dev = _device()
    [evt] = dev._parse_input(_button_packet(0x01, 0x0D, 0x01, 0x01))
    assert evt.kind == DeckEventKind.LCD_BUTTON
    assert evt.pos == 13


def test_parse_extra_hardware_buttons() -> None:
    dev = _device()
    [left] = dev._parse_input(_button_packet(0x01, 0x0F, 0x01, 0x01))
    assert left.kind == DeckEventKind.EXTRA_BUTTON
    assert left.pos == 14
    assert left.pressed is True

    [right] = dev._parse_input(_button_packet(0x01, 0x10, 0x01, 0x00))
    assert right.kind == DeckEventKind.EXTRA_BUTTON
    assert right.pos == 15
    assert right.pressed is False


def test_parse_encoder_press_release() -> None:
    dev = _device()
    for wire_idx, enc_idx in ((0x11, 0), (0x12, 1), (0x13, 2)):
        [press] = dev._parse_input(_button_packet(0x01, wire_idx, 0x02, 0x01))
        assert press.kind == DeckEventKind.ENCODER_PRESS
        assert press.encoder_index == enc_idx
        assert press.pressed is True

        [release] = dev._parse_input(_button_packet(0x01, wire_idx, 0x02, 0x00))
        assert release.kind == DeckEventKind.ENCODER_PRESS
        assert release.pressed is False


def test_parse_encoder_rotate() -> None:
    dev = _device()
    [ccw] = dev._parse_input(_button_packet(0x01, 0x11, 0x02, 0x02))
    assert ccw.kind == DeckEventKind.ENCODER_ROTATE
    assert ccw.encoder_index == 0
    assert ccw.delta == -1

    [cw] = dev._parse_input(_button_packet(0x01, 0x13, 0x02, 0x03))
    assert cw.kind == DeckEventKind.ENCODER_ROTATE
    assert cw.encoder_index == 2
    assert cw.delta == 1


def test_parse_unknown_marker_falls_back() -> None:
    dev = _device()
    [evt] = dev._parse_input(_button_packet(0x01, 0xFF, 0xAA, 0x00))
    assert evt.kind == DeckEventKind.UNKNOWN


def test_get_device_info_packet() -> None:
    pkt = build_packet(CommandProtocol.OUT_GET_DEVICE_INFO, b"")
    assert len(pkt) == PACKET_SIZE
    assert pkt[:2] == MAGIC
    assert pkt[2:4] == b"\x00\x03"
    assert pkt[4:8] == b"\x00\x00\x00\x00"


def test_enable_input_streaming_packet() -> None:
    pkt = build_packet(CommandProtocol.OUT_ENABLE_INPUT_STREAMING, b"")
    assert len(pkt) == PACKET_SIZE
    assert pkt[:2] == MAGIC
    assert pkt[2:4] == b"\x00\x02"
    assert pkt[4:8] == b"\x00\x00\x00\x00"
