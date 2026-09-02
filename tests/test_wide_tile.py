"""Smoke tests for wide-tile payload composition."""

from __future__ import annotations

from ulanzi_niri.protocol.ulanzi_d200x import SmallWindowMode
from ulanzi_niri.stats import StatsSnapshot
from ulanzi_niri.wide_tile import (
    build_background_payload,
    build_clock_payload,
    build_stats_payload,
)


def test_clock_payload_digital_time() -> None:
    p = build_clock_payload(SmallWindowMode.TIME, "%H:%M:%S")
    parts = p.split("|")
    assert parts[0] == str(int(SmallWindowMode.TIME))
    assert parts[1] == "0" and parts[2] == "0" and parts[4] == "0"
    assert parts[5] == "24H"
    assert parts[6] == ""


def test_clock_payload_time_date() -> None:
    p = build_clock_payload(SmallWindowMode.TIME_DATE)
    parts = p.split("|")
    assert parts[0] == str(int(SmallWindowMode.TIME_DATE))
    # suffix is a YYYY/MM/DD date
    assert len(parts[6].split("/")) == 3


def test_clock_payload_time_weekday() -> None:
    parts = build_clock_payload(SmallWindowMode.TIME_WEEKDAY).split("|")
    assert parts[6]  # non-empty weekday suffix


def test_stats_payload_format() -> None:
    snap = StatsSnapshot(cpu=42, mem=70, gpu=0, time="12:34:56")
    assert build_stats_payload(snap) == "0|42|70|12:34:56|0"


def test_background_payload() -> None:
    assert build_background_payload() == "2|"
