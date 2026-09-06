import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ulanzi_niri import ai_usage, service
from ulanzi_niri.config import (
    PROVIDER_URLS,
    BrightnessAction,
    ButtonEntry,
    Config,
    PageConfig,
    UsageWidget,
)
from ulanzi_niri.protocol.device import DeckEvent, DeckEventKind


@pytest.mark.parametrize(
    ("provider", "limit"),
    [
        ("claude", "five_hour"),
        ("codex", "seven_day"),
        ("opencode-go", "rolling"),
        ("moonshot", "balance"),
    ],
)
@pytest.mark.parametrize("custom_url", [None, "https://example.com/usage"])
async def test_widget_press_refreshes_usage_and_opens_url(monkeypatch, provider, limit, custom_url):
    widget = UsageWidget(pos=0, provider=provider, limit=limit, url=custom_url)
    cfg = Config(page=[PageConfig(name="usage", widget=[widget])])
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    app = service.Service("unused.toml")
    app._usage = Mock()
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch", dispatch)

    await app._on_button(DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=0, pressed=True))
    await app._on_button(DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=0, pressed=False))
    await app._on_button(DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=1, pressed=True))
    await asyncio.gather(*app._widget_tasks)

    app._usage.refresh.assert_called_once_with(force=True)
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0].url == (custom_url or PROVIDER_URLS[provider])


async def test_following_click_runs_while_widget_fetch_and_browser_are_pending(monkeypatch):
    release = asyncio.Event()
    fetch_started = asyncio.Event()
    browser_started = asyncio.Event()

    async def slow_fetch(timeout=20.0):
        fetch_started.set()
        await release.wait()
        return ai_usage.UsageFetchResult(ai_usage.FetchStatus.OK, {"providers": {}})

    async def slow_browser(action):
        browser_started.set()
        await release.wait()

    cfg = Config(
        page=[
            PageConfig(
                name="usage",
                widget=[UsageWidget(pos=0, provider="claude", limit="five_hour")],
                button=[ButtonEntry(pos=1, on_press=BrightnessAction(type="brightness", delta=-5))],
            )
        ]
    )
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    monkeypatch.setattr(ai_usage, "fetch_usage", slow_fetch)
    monkeypatch.setattr("ulanzi_niri.actions._do_url", slow_browser)
    app = service.Service("unused.toml")
    brightness = app._brightness

    class FakeDevice:
        async def __aiter__(self):
            yield DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=0, pressed=True)
            await fetch_started.wait()
            await browser_started.wait()
            yield DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=1, pressed=True)

        set_brightness = AsyncMock()

    app._device = FakeDevice()
    app._usage.set_on_update(None)
    try:
        await asyncio.wait_for(app._event_loop(), timeout=1)
        app._device.set_brightness.assert_awaited_once_with(brightness - 5)
        assert not app._usage._task.done()
        assert any(not task.done() for task in app._widget_tasks)
    finally:
        release.set()
        await asyncio.gather(*app._widget_tasks)
        if app._usage._task is not None:
            await app._usage._task


async def test_page_indicator_tracks_navigation_within_current_layer(monkeypatch):
    cfg = Config(
        page=[
            PageConfig(name="first", layer="home"),
            PageConfig(name="folder", layer="folder"),
            PageConfig(name="second", layer="home"),
        ]
    )
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    background = Mock(return_value=b"background")
    monkeypatch.setattr(service, "render_background", background)
    build_zip = Mock(return_value=b"zip")
    monkeypatch.setattr(service, "build_buttons_zip", build_zip)
    app = service.Service("unused.toml")
    app._device = Mock(push_buttons_zip=AsyncMock())
    monkeypatch.setattr(app, "_start_wide_tile_worker", Mock())

    await app._render_current_page()
    assert background.call_args.args[1:] == (0, 2)
    await app.cycle_page(1)
    assert background.call_args.args[1:] == (1, 2)
    await app.switch_page("folder")
    assert background.call_args.args[1:] == (0, 1)
    await app.page_back()
    assert background.call_args.args[1:] == (1, 2)
    assert build_zip.call_args.kwargs["wide_tile_image"] == b"background"
