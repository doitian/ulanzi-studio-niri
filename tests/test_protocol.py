"""Smoke tests for protocol packet framing."""

from __future__ import annotations

from ulanzi_niri.protocol.ulanzi_d200x import (
    HEADER_SIZE,
    MAGIC,
    PACKET_SIZE,
    PAYLOAD_SIZE,
    CommandProtocol,
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
