"""In-process CLI to Service page-command contract."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

from ulanzi_niri import cli, service
from ulanzi_niri.config import Config, PageConfig


async def test_next_page_moves_within_layer(monkeypatch, tmp_path, capsys):
    cfg = Config(
        page=[
            PageConfig(name="first", layer="home"),
            PageConfig(name="folder", layer="folder"),
            PageConfig(name="second", layer="home"),
        ]
    )
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    app = service.Service("unused.toml")
    monkeypatch.setattr(service, "render_background", Mock(return_value=b"bg"))
    monkeypatch.setattr(service, "build_buttons_zip", Mock(return_value=b"zip"))
    monkeypatch.setattr(app, "_start_wide_tile_worker", Mock())
    app._device = Mock(push_buttons_zip=AsyncMock())
    await app.start_control()
    try:
        assert await asyncio.to_thread(cli.main, ["next-page"]) == 0
        assert app._pages.name == "second"
        assert capsys.readouterr().out == "second\n"
    finally:
        await app.stop_control()


async def test_hardware_cycle_then_cli_back_shares_history(monkeypatch, tmp_path, capsys):
    cfg = Config(
        page=[
            PageConfig(name="first", layer="home"),
            PageConfig(name="folder", layer="folder"),
            PageConfig(name="second", layer="home"),
        ]
    )
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    app = service.Service("unused.toml")
    monkeypatch.setattr(service, "render_background", Mock(return_value=b"bg"))
    monkeypatch.setattr(service, "build_buttons_zip", Mock(return_value=b"zip"))
    monkeypatch.setattr(app, "_start_wide_tile_worker", Mock())
    app._device = Mock(push_buttons_zip=AsyncMock())
    await app.start_control()
    try:
        await app.cycle_page(1)
        assert app._pages.name == "second"
        assert await asyncio.to_thread(cli.main, ["back"]) == 0
        assert app._pages.name == "first"
        assert capsys.readouterr().out == "first\n"
    finally:
        await app.stop_control()
