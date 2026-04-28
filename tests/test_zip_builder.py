"""Smoke tests for the firmware-safe zip builder."""

from __future__ import annotations

from ulanzi_niri.config import ButtonEntry, LabelConfig
from ulanzi_niri.zip_builder import _zip_is_safe, build_buttons_zip


def test_safe_blob_alignment() -> None:
    # An empty (0x7c) byte at the offending offsets must be rejected
    bad = bytes(2048)
    bad = bad[:1016] + b"\x00" + bad[1017:]
    assert not _zip_is_safe(bad)
    good = b"X" * 4096
    assert _zip_is_safe(good)


def test_build_zip_produces_safe_blob() -> None:
    btns = [ButtonEntry(pos=i, label=f"B{i}") for i in [0, 1, 2, 5, 10, 12]]
    blob = build_buttons_zip(btns, LabelConfig())
    assert _zip_is_safe(blob)
    # Sanity: zip header magic
    assert blob[:2] == b"PK"
