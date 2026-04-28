"""Smoke tests for wide-tile payload composition."""

from __future__ import annotations

from ulanzi_niri.config import EncoderEntry
from ulanzi_niri.stats import StatsSnapshot
from ulanzi_niri.wide_tile import (
    build_background_payload,
    build_clock_payload,
    build_encoders_payload,
    build_stats_payload,
)


def test_clock_payload_format() -> None:
    p = build_clock_payload("%H:%M:%S")
    parts = p.split("|")
    assert parts[0] == "1"
    assert parts[1] == "0" and parts[2] == "0" and parts[4] == "0"


def test_stats_payload_format() -> None:
    snap = StatsSnapshot(cpu=42, mem=70, gpu=0, time="12:34:56")
    assert build_stats_payload(snap) == "0|42|70|12:34:56|0"


def test_background_payload() -> None:
    assert build_background_payload() == "2|"


def test_encoders_payload_three_slots() -> None:
    encs = [EncoderEntry(index=0, label="Vol"), EncoderEntry(index=2, label="Bri")]
    payload = build_encoders_payload(encs, {0: "42%", 2: "60"})
    parts = payload.split("|")
    assert parts[0] == "3"
    # 3 (label, value) pairs even when middle encoder is missing
    assert len(parts) == 1 + 3 * 2
    assert parts[1] == "Vol" and parts[2] == "42%"
    assert parts[3] == "E1" and parts[4] == ""
    assert parts[5] == "Bri" and parts[6] == "60"
