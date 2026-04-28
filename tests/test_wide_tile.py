"""Smoke tests for wide-tile payload composition."""

from __future__ import annotations

from ulanzi_niri.stats import StatsSnapshot
from ulanzi_niri.wide_tile import (
    build_background_payload,
    build_clock_payload,
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
