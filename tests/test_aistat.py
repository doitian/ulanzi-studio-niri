"""Tests for the aistat usage fetcher/renderer."""

from __future__ import annotations

import asyncio
import io
import json

from PIL import Image

from ulanzi_niri import aistat
from ulanzi_niri.aistat import (
    FetchStatus,
    UsageFetcher,
    _default_label,
    format_reset,
    render_widget,
    resolve_limit,
)
from ulanzi_niri.config import AistatWidget

PROVIDERS = {
    "claude": {
        "accounts": [
            {
                "email": "ian@example.com",
                "active": True,
                "limits": {
                    "five_hour": {
                        "used_percent": 20,
                        "remaining_percent": 80,
                        "resets_at": "2026-09-02T04:50:00+00:00",
                        "reset_after_seconds": 1461,
                    },
                    "seven_day": {
                        "used_percent": 3,
                        "remaining_percent": 97,
                        "resets_at": "2026-09-03T08:00:00+00:00",
                        "reset_after_seconds": 99261,
                    },
                    "seven_day_fable": {
                        "used_percent": 2,
                        "remaining_percent": 98,
                        "resets_at": "2026-09-03T08:00:00+00:00",
                        "reset_after_seconds": 99261,
                    },
                },
            }
        ]
    },
    "codex": {
        "accounts": [
            {
                "email": "me@pomail.net",
                "active": True,
                "limits": {
                    "five_hour": {
                        "used_percent": 3,
                        "remaining_percent": 97,
                        "resets_at": "2026-09-02T09:20:05+00:00",
                        "reset_after_seconds": 17666,
                    },
                    "seven_day": {
                        "used_percent": 57,
                        "remaining_percent": 43,
                        "resets_at": "2026-09-07T03:42:00+00:00",
                        "reset_after_seconds": 429381,
                    },
                },
            }
        ]
    },
}


def test_resolve_limit_by_email() -> None:
    info = resolve_limit(PROVIDERS, "codex", "me@pomail.net", "seven_day")
    assert info is not None
    assert info.remaining_percent == 43
    assert info.reset_after_seconds == 429381


def test_resolve_limit_active_fallback() -> None:
    info = resolve_limit(PROVIDERS, "claude", "", "five_hour")
    assert info is not None
    assert info.remaining_percent == 80


def test_resolve_claude_fable_limit() -> None:
    info = resolve_limit(PROVIDERS, "claude", "", "seven_day_fable")
    assert info is not None
    assert info.remaining_percent == 98


def test_claude_fable_default_label() -> None:
    widget = AistatWidget(pos=1, provider="claude", limit="seven_day_fable")
    assert _default_label(widget) == "clfable"


def test_resolve_limit_missing() -> None:
    assert resolve_limit(PROVIDERS, "codex", "nobody@x.com", "five_hour") is None
    assert resolve_limit(PROVIDERS, "claude", "ian@example.com", "thirty_day") is None
    assert resolve_limit({}, "claude", "", "five_hour") is None


def test_format_reset() -> None:
    assert format_reset(0) == "now"
    assert format_reset(-5) == "now"
    assert format_reset(53) == "<1m"
    assert format_reset(1461) == "24m"
    assert format_reset(17666) == "4h"
    assert format_reset(99261) == "1d"
    assert format_reset(429381) == "4d"


def test_render_widget_dimensions() -> None:
    widget = AistatWidget(
        pos=1, provider="claude", account="", limit="five_hour", label="Claude 5h"
    )
    png = render_widget(widget, PROVIDERS)
    img = Image.open(io.BytesIO(png))
    assert img.size == (196, 196)


def test_render_widget_missing_data() -> None:
    widget = AistatWidget(
        pos=1, provider="codex", account="nobody@x.com", limit="five_hour", label="Codex 5h"
    )
    png = render_widget(widget, PROVIDERS)
    assert Image.open(io.BytesIO(png)).size == (196, 196)


def test_render_widget_background_icon(tmp_path, monkeypatch) -> None:
    icon = tmp_path / "app.png"
    Image.new("RGB", (64, 64), (255, 255, 255)).save(icon)

    widget = AistatWidget(
        pos=1,
        provider="claude",
        account="",
        limit="five_hour",
        label="Claude 5h",
        background="app",
    )

    monkeypatch.setattr(aistat, "resolve_icon_path", lambda _name: str(icon))

    img = Image.open(io.BytesIO(render_widget(widget, PROVIDERS))).convert("RGB")
    assert img.size == (196, 196)
    assert img.getpixel((0, 0)) != (0, 0, 0)


def test_render_widget_missing_background_falls_back_to_black(monkeypatch) -> None:
    widget = AistatWidget(
        pos=1,
        provider="claude",
        account="",
        limit="five_hour",
        label="Claude 5h",
        background="nope",
    )

    monkeypatch.setattr(aistat, "resolve_icon_path", lambda _name: None)

    img = Image.open(io.BytesIO(render_widget(widget, PROVIDERS))).convert("RGB")
    assert img.getpixel((0, 0)) == (0, 0, 0)


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:
        pass

    async def wait(self) -> None:
        pass


async def test_fetch_usage_parses_json(monkeypatch) -> None:
    payload = json.dumps({"providers": PROVIDERS}).encode()

    async def fake_exec(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(payload)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await aistat.fetch_usage()
    assert result.status is FetchStatus.OK
    assert result.data == {"providers": PROVIDERS}


async def test_fetch_usage_missing_binary(monkeypatch) -> None:
    async def fake_exec(*_args, **_kwargs) -> _FakeProc:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await aistat.fetch_usage()
    assert result.status is FetchStatus.ERROR
    assert result.data is None


async def test_fetch_usage_bad_json(monkeypatch) -> None:
    async def fake_exec(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(b"not json", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await aistat.fetch_usage()
    assert result.status is FetchStatus.ERROR
    assert result.data is None


async def test_fetch_usage_timeout(monkeypatch) -> None:
    async def fake_exec(*_args, **_kwargs) -> _FakeProc:
        return _FakeProc(b"", returncode=0)

    async def fake_wait_for(coro, timeout=None):
        await coro
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    result = await aistat.fetch_usage(timeout=0.1)
    assert result.status is FetchStatus.TIMEOUT
    assert result.data is None


async def test_usage_fetcher_refreshes_in_background(monkeypatch) -> None:
    calls = 0

    async def fake_fetch(timeout: float = 20.0) -> aistat.UsageFetchResult:
        nonlocal calls
        calls += 1
        return aistat.UsageFetchResult(FetchStatus.OK, {"providers": PROVIDERS})

    monkeypatch.setattr(aistat, "fetch_usage", fake_fetch)

    updates = 0

    async def on_update() -> None:
        nonlocal updates
        updates += 1

    fetcher = UsageFetcher()
    fetcher.set_on_update(on_update)
    assert fetcher.get() is None

    fetcher.refresh()
    assert fetcher._task is not None
    await fetcher._task
    assert fetcher.get() is not None
    assert updates == 1

    fetcher.refresh()
    assert calls == 1

    fetcher.refresh(force=True)
    assert fetcher._task is not None
    await fetcher._task
    assert calls == 2
