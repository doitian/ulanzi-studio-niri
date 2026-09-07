import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from ulanzi_niri import ai_usage, service
from ulanzi_niri.config import (
    PROVIDER_URLS,
    BrightnessAction,
    ButtonEntry,
    Config,
    DeviceConfig,
    EncoderEntry,
    MediaAction,
    NoopAction,
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


def _encoder_service(monkeypatch, *encoders: EncoderEntry, pages: list[PageConfig] | None = None):
    if pages is None:
        pages = [PageConfig(name="main", encoder=list(encoders))]
    cfg = Config(device=DeviceConfig(encoder_coalesce_ms=0), page=pages)
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    app = service.Service("unused.toml")
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch", dispatch)
    return app, dispatch


async def _drain_encoders(app: service.Service) -> None:
    tasks = [t for t in app._encoder_flush_task.values() if not t.done()]
    if tasks:
        await asyncio.gather(*tasks)


def _press(idx: int, pressed: bool) -> DeckEvent:
    return DeckEvent(kind=DeckEventKind.ENCODER_PRESS, encoder_index=idx, pressed=pressed)


def _rotate(idx: int, delta: int, *, held: bool = False) -> DeckEvent:
    return DeckEvent(
        kind=DeckEventKind.ENCODER_ROTATE,
        encoder_index=idx,
        delta=delta,
        pressed=held,
    )


async def test_encoder_click_fires_on_release_not_press(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    app, dispatch = _encoder_service(monkeypatch, EncoderEntry(index=0, on_press=mute))

    await app._on_encoder_press(_press(0, True))
    dispatch.assert_not_awaited()

    await app._on_encoder_press(_press(0, False))
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0] is mute
    assert dispatch.call_args.args[1].source == "encoder:0:press"


async def test_encoder_hold_rotate_bound_cancels_click(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    nxt = MediaAction(type="media", cmd="next")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press=mute, on_press_rotate_cw=nxt),
    )

    await app._on_encoder_press(_press(0, True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await app._on_encoder_press(_press(0, False))
    await _drain_encoders(app)

    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0] is nxt
    assert dispatch.call_args.args[1].source == "encoder:0:press_cw"


async def test_encoder_hold_rotate_falls_through_and_cancels_click(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    vol = MediaAction(type="media", cmd="vol-up")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press=mute, on_rotate_cw=vol),
    )

    await app._on_encoder_press(_press(0, True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await app._on_encoder_press(_press(0, False))
    await _drain_encoders(app)

    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0] is vol
    assert dispatch.call_args.args[1].source == "encoder:0:press_cw"


async def test_encoder_hold_rotate_noop_does_not_fall_through(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    vol = MediaAction(type="media", cmd="vol-up")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(
            index=0,
            on_press=mute,
            on_rotate_cw=vol,
            on_press_rotate_cw=NoopAction(type="noop"),
        ),
    )

    await app._on_encoder_press(_press(0, True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await app._on_encoder_press(_press(0, False))
    await _drain_encoders(app)

    dispatch.assert_awaited_once()
    assert isinstance(dispatch.call_args.args[0], NoopAction)


async def test_encoder_hold_rotate_unmapped_direction_falls_through(monkeypatch):
    nxt = MediaAction(type="media", cmd="next")
    vol_down = MediaAction(type="media", cmd="vol-down")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press_rotate_cw=nxt, on_rotate_ccw=vol_down),
    )

    await app._on_encoder_press(_press(0, True))
    app._on_encoder_rotate(_rotate(0, -1, held=True))
    await _drain_encoders(app)

    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0] is vol_down
    assert dispatch.call_args.args[1].source == "encoder:0:press_ccw"


async def test_encoder_click_on_release_without_press_rotate_keys(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    app, dispatch = _encoder_service(monkeypatch, EncoderEntry(index=0, on_press=mute))

    await app._on_encoder_press(_press(0, True))
    dispatch.assert_not_awaited()
    await app._on_encoder_press(_press(0, False))
    dispatch.assert_awaited_once()


async def test_encoder_free_rotate_does_not_cancel_later_click(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    vol = MediaAction(type="media", cmd="vol-up")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press=mute, on_rotate_cw=vol),
    )

    app._on_encoder_rotate(_rotate(0, 1, held=False))
    await _drain_encoders(app)
    await app._on_encoder_press(_press(0, True))
    await app._on_encoder_press(_press(0, False))

    assert dispatch.await_count == 2
    assert dispatch.call_args_list[0].args[0] is vol
    assert dispatch.call_args_list[1].args[0] is mute


async def test_encoder_hold_on_one_does_not_cancel_click_on_another(monkeypatch):
    mute0 = MediaAction(type="media", cmd="mute")
    nxt = MediaAction(type="media", cmd="next")
    mute1 = MediaAction(type="media", cmd="play-pause")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press=mute0, on_press_rotate_cw=nxt),
        EncoderEntry(index=1, on_press=mute1),
    )

    await app._on_encoder_press(_press(0, True))
    await app._on_encoder_press(_press(1, True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await app._on_encoder_press(_press(0, False))
    await app._on_encoder_press(_press(1, False))
    await _drain_encoders(app)

    actions = [c.args[0] for c in dispatch.call_args_list]
    assert nxt in actions
    assert mute1 in actions
    assert mute0 not in actions


async def test_encoder_hold_and_free_pulses_use_separate_buckets(monkeypatch):
    nxt = MediaAction(type="media", cmd="next")
    vol = MediaAction(type="media", cmd="vol-up")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press_rotate_cw=nxt, on_rotate_cw=vol),
    )

    app._on_encoder_rotate(_rotate(0, 1, held=False))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await _drain_encoders(app)

    actions = [c.args[0] for c in dispatch.call_args_list]
    assert actions == [vol, nxt]


async def test_encoder_hold_rotate_cancels_click_without_rotate_action(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    app, dispatch = _encoder_service(monkeypatch, EncoderEntry(index=0, on_press=mute))

    await app._on_encoder_press(_press(0, True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await app._on_encoder_press(_press(0, False))
    await _drain_encoders(app)

    dispatch.assert_not_awaited()


async def test_encoder_coalesce_repeats_hold_rotate_action(monkeypatch):
    nxt = MediaAction(type="media", cmd="next")
    app, dispatch = _encoder_service(
        monkeypatch,
        EncoderEntry(index=0, on_press_rotate_cw=nxt),
    )

    app._on_encoder_rotate(_rotate(0, 1, held=True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await _drain_encoders(app)

    assert dispatch.await_count == 2
    assert all(c.args[0] is nxt for c in dispatch.call_args_list)


async def test_encoder_missing_mapping_is_noop(monkeypatch):
    app, dispatch = _encoder_service(monkeypatch)

    await app._on_encoder_press(_press(0, True))
    app._on_encoder_rotate(_rotate(0, 1, held=True))
    await _drain_encoders(app)
    await app._on_encoder_press(_press(0, False))

    dispatch.assert_not_awaited()


async def test_encoder_page_switch_swallows_in_flight_click(monkeypatch):
    mute = MediaAction(type="media", cmd="mute")
    app, dispatch = _encoder_service(
        monkeypatch,
        pages=[
            PageConfig(name="main", encoder=[EncoderEntry(index=0, on_press=mute)]),
            PageConfig(name="apps"),
        ],
    )

    await app._on_encoder_press(_press(0, True))
    app._pages.switch("apps")
    await app._on_encoder_press(_press(0, False))

    dispatch.assert_not_awaited()
