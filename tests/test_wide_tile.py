"""Smoke tests for wide-tile payload composition."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from ulanzi_niri.config import WideTileEntry
from ulanzi_niri.protocol.ulanzi_d200x import SmallWindowMode
from ulanzi_niri.stats import StatsSnapshot
from ulanzi_niri.wide_tile import (
    build_background_payload,
    build_clock_payload,
    build_stats_payload,
    render_background,
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


@pytest.mark.parametrize("index", [0, 1, 2])
def test_page_indicator_position_and_colors(index) -> None:
    image = Image.open(io.BytesIO(render_background(WideTileEntry(), index, 3)))
    assert image.size == (458, 196)
    for x in range(458):
        expected = (112, 112, 112) if 12 <= x < 446 else (0, 0, 0)
        if 12 + index * 434 // 3 <= x < 12 + (index + 1) * 434 // 3:
            expected = (153, 116, 248)
        for y in range(184, 192):
            assert image.getpixel((x, y)) == expected
        assert image.getpixel((x, 183)) == (0, 0, 0)
        assert image.getpixel((x, 192)) == (0, 0, 0)


def test_single_page_has_no_indicator() -> None:
    image = Image.open(io.BytesIO(render_background(WideTileEntry(), 0, 1)))
    assert image.getbbox() is None


def test_page_indicator_preserves_background_image(tmp_path) -> None:
    path = tmp_path / "background.png"
    Image.new("RGB", (458, 196), "red").save(path)
    cfg = WideTileEntry(mode="background", image=str(path))
    single = Image.open(io.BytesIO(render_background(cfg, 0, 1)))
    multiple = Image.open(io.BytesIO(render_background(cfg, 1, 2)))
    assert single.getpixel((229, 190)) == (255, 0, 0)
    assert multiple.getpixel((229, 190)) == (153, 116, 248)
    assert multiple.getpixel((0, 0)) == (255, 0, 0)
