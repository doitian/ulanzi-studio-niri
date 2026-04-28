"""Smoke tests for icon resolution and render-cache keying."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ulanzi_niri.config import LabelConfig
from ulanzi_niri.icons import RenderRequest, request_from_button, resolve_icon_path


def _make_icon(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


@pytest.fixture
def isolated_icon_search(tmp_path: Path):
    """Confine resolve_icon_path to a fake set of roots, with cache cleared."""
    empty = tmp_path / "_empty"
    empty.mkdir()
    resolve_icon_path.cache_clear()
    with patch("ulanzi_niri.icons._user_icons_dir", return_value=empty), \
         patch("ulanzi_niri.icons._bundled_icons_dir", return_value=empty):
        yield
    resolve_icon_path.cache_clear()


def test_resolve_bare_name_prefers_largest_size(tmp_path: Path, isolated_icon_search) -> None:
    root = tmp_path / "icons"
    _make_icon(root / "hicolor" / "16x16" / "apps" / "firefox.png")
    _make_icon(root / "hicolor" / "256x256" / "apps" / "firefox.png")
    _make_icon(root / "hicolor" / "48x48" / "apps" / "firefox.png")

    with patch("ulanzi_niri.icons._system_icon_roots", return_value=(root,)):
        hit = resolve_icon_path("firefox")
    assert hit is not None
    assert hit.parent.name == "apps"
    assert "256x256" in hit.parts


def test_resolve_filename_walks_recursively(tmp_path: Path, isolated_icon_search) -> None:
    root = tmp_path / "icons"
    _make_icon(root / "deep" / "nested" / "thing" / "logo.png")

    with patch("ulanzi_niri.icons._system_icon_roots", return_value=(root,)):
        hit = resolve_icon_path("logo.png")
    assert hit is not None
    assert hit.name == "logo.png"


def test_resolve_returns_none_when_missing(tmp_path: Path, isolated_icon_search) -> None:
    with patch("ulanzi_niri.icons._system_icon_roots", return_value=(tmp_path,)):
        assert resolve_icon_path("nope") is None


def test_render_request_key_changes_with_mtime() -> None:
    a = RenderRequest(label="x", icon="i", width=10, height=10, icon_path="/p", icon_mtime=1.0)
    b = RenderRequest(label="x", icon="i", width=10, height=10, icon_path="/p", icon_mtime=2.0)
    assert repr(a) != repr(b)


def test_request_from_button_resolves_and_stats(tmp_path: Path, isolated_icon_search) -> None:
    icon = tmp_path / "ok.png"
    _make_icon(icon)
    label_cfg = LabelConfig()

    with patch("ulanzi_niri.icons._system_icon_roots", return_value=(tmp_path,)):
        req = request_from_button("Ok", "ok.png", 196, 196, label_cfg)
    assert req.icon_path == str(icon)
    assert req.icon_mtime is not None and req.icon_mtime > 0


def test_request_from_button_missing_icon_is_none(tmp_path: Path, isolated_icon_search) -> None:
    label_cfg = LabelConfig()
    with patch("ulanzi_niri.icons._system_icon_roots", return_value=(tmp_path,)):
        req = request_from_button("Ok", "missing.png", 196, 196, label_cfg)
    assert req.icon_path is None
    assert req.icon_mtime is None
