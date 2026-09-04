"""Tests for the plan-usage fetcher and renderer."""

from __future__ import annotations

import base64
import io
import json

from PIL import Image

from ulanzi_niri import ai_usage
from ulanzi_niri.ai_usage import (
    FetchStatus,
    UsageFetcher,
    _default_label,
    format_reset,
    render_widget,
    resolve_auth_error,
    resolve_http_status,
    resolve_limit,
    result_auth_denied,
)
from ulanzi_niri.config import UsageWidget

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
    "opencode-go": {
        "accounts": [
            {
                "email": "",
                "active": True,
                "limits": {
                    "rolling": {
                        "used_percent": 25,
                        "remaining_percent": 75,
                        "resets_at": "2026-09-02T05:00:00+00:00",
                        "reset_after_seconds": 3600,
                    },
                    "weekly": {
                        "used_percent": 40,
                        "remaining_percent": 60,
                        "resets_at": "2026-09-08T00:00:00+00:00",
                        "reset_after_seconds": 500000,
                    },
                    "monthly": {
                        "used_percent": 10,
                        "remaining_percent": 90,
                        "resets_at": "2026-10-01T00:00:00+00:00",
                        "reset_after_seconds": 2000000,
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


def test_resolve_limit_anonymous_active_credential() -> None:
    providers = {
        "claude": {
            "accounts": [
                {
                    "email": "",
                    "active": True,
                    "limits": PROVIDERS["claude"]["accounts"][0]["limits"],
                }
            ]
        }
    }
    info = resolve_limit(providers, "claude", "previous-config@example.com", "five_hour")
    assert info is not None
    assert info.remaining_percent == 80


def test_resolve_claude_fable_limit() -> None:
    info = resolve_limit(PROVIDERS, "claude", "", "seven_day_fable")
    assert info is not None
    assert info.remaining_percent == 98


def test_default_labels() -> None:
    assert _default_label(UsageWidget(pos=1, provider="claude", limit="five_hour")) == "5H"
    assert _default_label(UsageWidget(pos=1, provider="codex", limit="seven_day")) == "7D"
    assert _default_label(UsageWidget(pos=1, provider="claude", limit="seven_day_fable")) == "FABLE"
    assert _default_label(UsageWidget(pos=1, provider="opencode-go", limit="rolling")) == "5H"
    assert _default_label(UsageWidget(pos=1, provider="opencode-go", limit="weekly")) == "7D"
    assert _default_label(UsageWidget(pos=1, provider="opencode-go", limit="monthly")) == "30D"


def test_resolve_limit_missing() -> None:
    assert resolve_limit(PROVIDERS, "codex", "nobody@x.com", "five_hour") is None
    assert resolve_limit(PROVIDERS, "claude", "ian@example.com", "thirty_day") is None
    assert resolve_limit({}, "claude", "", "five_hour") is None


def test_resolve_auth_error_account() -> None:
    providers = {
        "claude": {
            "accounts": [
                {
                    "email": "ian@example.com",
                    "active": True,
                    "limits": None,
                    "error": "auth denied: HTTP 401 from https://api.anthropic.com: ...",
                }
            ]
        }
    }
    assert resolve_auth_error(providers, "claude", "") is not None


def test_resolve_auth_error_provider_level() -> None:
    providers = {
        "claude": {
            "error": "auth missing: claude token not found — run `claude /login` to authenticate"
        }
    }
    assert resolve_auth_error(providers, "claude", "") is not None


def test_resolve_auth_error_none() -> None:
    assert resolve_auth_error(PROVIDERS, "claude", "") is None
    assert resolve_auth_error({}, "claude", "") is None


def test_result_auth_denied() -> None:
    assert result_auth_denied(None) is False
    assert result_auth_denied({"providers": PROVIDERS}) is False
    assert (
        result_auth_denied(
            {"providers": {"claude": {"accounts": [{"error": "auth denied: HTTP 401"}]}}}
        )
        is True
    )
    assert result_auth_denied({"providers": {"claude": {"error": "auth missing"}}}) is True


def test_render_widget_auth_error() -> None:
    providers = {
        "claude": {
            "accounts": [
                {
                    "email": "ian@example.com",
                    "active": True,
                    "limits": None,
                    "error": "auth denied: HTTP 401 from https://api.anthropic.com",
                }
            ]
        }
    }
    widget = UsageWidget(pos=1, provider="claude", account="", limit="five_hour")
    png = render_widget(widget, providers)
    assert Image.open(io.BytesIO(png)).size == (196, 196)


def test_resolve_http_status() -> None:
    providers = {
        "claude": {
            "accounts": [
                {
                    "email": "ian@example.com",
                    "active": True,
                    "limits": None,
                    "error": "auth denied: HTTP 403 from https://api.anthropic.com: forbidden",
                }
            ]
        },
        "codex": {
            "accounts": [
                {
                    "email": "me@pomail.net",
                    "active": True,
                    "limits": None,
                    "error": "transient failure: HTTP 429 from https://api.openai.com: slow down",
                }
            ]
        },
    }
    assert resolve_http_status(providers, "claude", "") == 403
    assert resolve_http_status(providers, "codex", "") == 429


def test_resolve_http_status_provider_level() -> None:
    providers = {"claude": {"error": "transient failure: HTTP 500 from https://api.anthropic.com"}}
    assert resolve_http_status(providers, "claude", "") == 500


def test_resolve_http_status_none() -> None:
    assert resolve_http_status(PROVIDERS, "claude", "") is None
    assert resolve_http_status({}, "claude", "") is None
    assert (
        resolve_http_status(
            {"claude": {"error": "auth missing: claude token not found"}}, "claude", ""
        )
        is None
    )


def test_resolve_http_status_isolated_per_provider() -> None:
    providers = {
        "claude": {
            "accounts": [
                {"email": "ian@example.com", "active": True, "error": "auth denied: HTTP 401"}
            ]
        },
        "codex": PROVIDERS["codex"],
    }
    assert resolve_http_status(providers, "claude", "") == 401
    assert resolve_http_status(providers, "codex", "") is None


def test_format_reset() -> None:
    assert format_reset(0) == "now"
    assert format_reset(-5) == "now"
    assert format_reset(53) == "<1m"
    assert format_reset(1461) == "24m"
    assert format_reset(17666) == "4h54m"
    assert format_reset(99261) == "1d3h"
    assert format_reset(429381) == "4d23h"


def test_render_widget_dimensions() -> None:
    widget = UsageWidget(pos=1, provider="claude", account="", limit="five_hour", label="Claude 5h")
    png = render_widget(widget, PROVIDERS)
    img = Image.open(io.BytesIO(png))
    assert img.size == (196, 196)


def test_render_widget_missing_data() -> None:
    widget = UsageWidget(
        pos=1, provider="codex", account="nobody@x.com", limit="five_hour", label="Codex 5h"
    )
    png = render_widget(widget, PROVIDERS)
    assert Image.open(io.BytesIO(png)).size == (196, 196)


def test_render_widget_keeps_provider_data_when_other_provider_failed(monkeypatch) -> None:
    drawn = []
    monkeypatch.setattr(
        ai_usage, "_draw_fit", lambda _draw, _center, text, *_args, **_kwargs: drawn.append(text)
    )
    widget = UsageWidget(pos=1, provider="claude", account="", limit="five_hour")

    render_widget(widget, PROVIDERS, status=FetchStatus.ERROR)

    assert "80%" in drawn
    assert "Err" not in drawn


def test_render_widget_corner_icon(tmp_path, monkeypatch) -> None:
    icon = tmp_path / "app.png"
    Image.new("RGB", (64, 64), (255, 255, 255)).save(icon)

    widget = UsageWidget(
        pos=1,
        provider="claude",
        account="",
        limit="five_hour",
        icon="app",
    )

    monkeypatch.setattr(ai_usage, "resolve_icon_path", lambda _name: str(icon))

    img = Image.open(io.BytesIO(render_widget(widget, PROVIDERS))).convert("RGB")
    assert img.size == (196, 196)
    assert img.getpixel((164, 32)) != (0, 0, 0)
    assert img.getpixel((0, 0)) == (0, 0, 0)


def test_render_widget_missing_icon_falls_back_to_black(monkeypatch) -> None:
    widget = UsageWidget(
        pos=1,
        provider="claude",
        account="",
        limit="five_hour",
        icon="nope",
    )

    monkeypatch.setattr(ai_usage, "resolve_icon_path", lambda _name: None)

    img = Image.open(io.BytesIO(render_widget(widget, PROVIDERS))).convert("RGB")
    assert img.getpixel((0, 0)) == (0, 0, 0)


async def test_fetch_usage_combines_providers(monkeypatch) -> None:
    async def immediate(func, *args):
        return func(*args)

    monkeypatch.setattr(ai_usage.asyncio, "to_thread", immediate)
    monkeypatch.setattr(ai_usage, "_fetch_claude", lambda _timeout: PROVIDERS["claude"])
    monkeypatch.setattr(ai_usage, "_fetch_codex", lambda _timeout: PROVIDERS["codex"])
    monkeypatch.setattr(ai_usage, "_fetch_opencode_go", lambda _timeout: PROVIDERS["opencode-go"])
    result = await ai_usage.fetch_usage()
    assert result.status is FetchStatus.OK
    assert result.data == {"providers": PROVIDERS}


async def test_fetch_usage_preserves_provider_error(monkeypatch) -> None:
    async def immediate(func, *args):
        return func(*args)

    monkeypatch.setattr(ai_usage.asyncio, "to_thread", immediate)
    error = {"error": "auth missing: codex token not found"}
    monkeypatch.setattr(ai_usage, "_fetch_claude", lambda _timeout: PROVIDERS["claude"])
    monkeypatch.setattr(ai_usage, "_fetch_codex", lambda _timeout: error)
    monkeypatch.setattr(ai_usage, "_fetch_opencode_go", lambda _timeout: PROVIDERS["opencode-go"])
    result = await ai_usage.fetch_usage()
    assert result.status is FetchStatus.ERROR
    assert result.data == {
        "providers": {
            "claude": PROVIDERS["claude"],
            "codex": error,
            "opencode-go": PROVIDERS["opencode-go"],
        }
    }


def test_codex_window_key() -> None:
    assert ai_usage._codex_window_key(18000) == "five_hour"
    assert ai_usage._codex_window_key(604800) == "seven_day"
    assert ai_usage._codex_window_key(123) == "window_123s"


def test_opencode_go_key_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_GO_API_KEY", " test-key ")
    assert ai_usage._opencode_go_key() == "test-key"


def test_opencode_go_key_from_auth_file(monkeypatch, tmp_path) -> None:
    auth_dir = tmp_path / "opencode"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "stored-key"}})
    )
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert ai_usage._opencode_go_key() == "stored-key"


def test_fetch_opencode_go(monkeypatch) -> None:
    monkeypatch.setattr(ai_usage, "_opencode_go_key", lambda: "test-key")
    monkeypatch.setattr(
        ai_usage,
        "_request_json",
        lambda *_args, **_kwargs: {
            "usage": {
                "rolling": {"status": "ok", "percent": 25, "resetsAt": "2099-01-01T00:00:00Z"},
                "weekly": {"status": "ok", "percent": 40, "resetsAt": "2099-01-02T00:00:00Z"},
                "monthly": {"status": "ok", "percent": 10, "resetsAt": "2099-02-01T00:00:00Z"},
            }
        },
    )

    provider = ai_usage._fetch_opencode_go(5)
    limits = provider["accounts"][0]["limits"]

    assert limits["rolling"]["remaining_percent"] == 75
    assert limits["weekly"]["remaining_percent"] == 60
    assert limits["monthly"]["remaining_percent"] == 90


def test_jwt_email() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"email": "me@example.com"}).encode()).rstrip(
        b"="
    )
    assert ai_usage._jwt_email(f"header.{payload.decode()}.signature") == "me@example.com"
    assert ai_usage._jwt_email("not-a-jwt") == ""


def test_claude_refreshes_expired_token(monkeypatch, tmp_path) -> None:
    credential = {
        "claudeAiOauth": {
            "accessToken": "old-access",
            "refreshToken": "old-refresh",
            "expiresAt": 1,
        },
        "preserved": True,
    }
    written = []
    monkeypatch.setattr(
        ai_usage,
        "_refresh_token",
        lambda *_args: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        ai_usage,
        "_write_json_atomic",
        lambda _path, value, **_kwargs: written.append(value),
    )

    token = ai_usage._claude_access_token(tmp_path / "credentials.json", credential, 5)

    assert token == "new-access"
    assert credential["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert credential["preserved"] is True
    assert written == [credential]


def test_codex_refreshes_expired_token(monkeypatch, tmp_path) -> None:
    payload = base64.urlsafe_b64encode(b'{"exp":1}').rstrip(b"=").decode()
    credential = {
        "tokens": {
            "access_token": f"header.{payload}.signature",
            "refresh_token": "old-refresh",
            "id_token": "old-id",
        }
    }
    monkeypatch.setattr(
        ai_usage,
        "_refresh_token",
        lambda *_args: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
        },
    )
    monkeypatch.setattr(ai_usage, "_write_json_atomic", lambda *_args, **_kwargs: None)

    token = ai_usage._codex_access_token(tmp_path / "auth.json", credential, 5)

    assert token == "new-access"
    assert credential["tokens"]["refresh_token"] == "new-refresh"
    assert credential["tokens"]["id_token"] == "new-id"


def test_rate_limited_result_is_detected() -> None:
    data = {"providers": {"claude": {"error": "request failed: rate limited: HTTP 429"}}}
    assert ai_usage.result_rate_limited(data) is True


async def test_fetch_usage_timeout(monkeypatch) -> None:
    async def immediate(func, *args):
        return func(*args)

    def timed_out(_timeout: float) -> dict:
        raise TimeoutError

    monkeypatch.setattr(ai_usage.asyncio, "to_thread", immediate)
    monkeypatch.setattr(ai_usage, "_fetch_claude", timed_out)
    monkeypatch.setattr(ai_usage, "_fetch_codex", timed_out)
    monkeypatch.setattr(ai_usage, "_fetch_opencode_go", timed_out)
    result = await ai_usage.fetch_usage(timeout=0.1)
    assert result.status is FetchStatus.TIMEOUT
    assert result.data is None


async def test_usage_fetcher_refreshes_in_background(monkeypatch) -> None:
    calls = 0

    async def fake_fetch(timeout: float = 20.0) -> ai_usage.UsageFetchResult:
        nonlocal calls
        calls += 1
        return ai_usage.UsageFetchResult(FetchStatus.OK, {"providers": PROVIDERS})

    monkeypatch.setattr(ai_usage, "fetch_usage", fake_fetch)

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
    assert calls == 1

    assert fetcher._fetched_at is not None
    fetcher._fetched_at -= ai_usage.MANUAL_REFRESH_THROTTLE_SECONDS
    fetcher.refresh(force=True)
    assert fetcher._task is not None
    await fetcher._task
    assert calls == 2


async def test_usage_fetcher_retries_after_failure(monkeypatch) -> None:
    calls = 0

    async def fake_fetch(timeout: float = 20.0) -> ai_usage.UsageFetchResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ai_usage.UsageFetchResult(FetchStatus.ERROR)
        return ai_usage.UsageFetchResult(FetchStatus.OK, {"providers": PROVIDERS})

    monkeypatch.setattr(ai_usage, "fetch_usage", fake_fetch)
    monkeypatch.setattr(ai_usage, "RETRY_BACKOFF_SECONDS", (0,))

    fetcher = UsageFetcher()
    fetcher.refresh()
    await fetcher._task
    assert fetcher.get().status is FetchStatus.ERROR
    assert fetcher._retry_task is not None

    retry_task = fetcher._retry_task
    await retry_task
    assert fetcher.get().status is FetchStatus.OK
    assert calls == 2


async def test_usage_fetcher_does_not_retry_auth_error(monkeypatch) -> None:
    async def fake_fetch(timeout: float = 20.0) -> ai_usage.UsageFetchResult:
        return ai_usage.UsageFetchResult(
            FetchStatus.ERROR,
            {
                "providers": {
                    "claude": {
                        "error": "auth missing: claude token not found — run `claude /login` to authenticate"
                    }
                }
            },
        )

    monkeypatch.setattr(ai_usage, "fetch_usage", fake_fetch)
    monkeypatch.setattr(ai_usage, "RETRY_BACKOFF_SECONDS", (0,))

    fetcher = UsageFetcher()
    fetcher.refresh()
    await fetcher._task
    assert fetcher.get().status is FetchStatus.ERROR
    assert fetcher._retry_task is None


async def test_usage_fetcher_does_not_retry_rate_limit(monkeypatch) -> None:
    async def fake_fetch(timeout: float = 20.0) -> ai_usage.UsageFetchResult:
        return ai_usage.UsageFetchResult(
            FetchStatus.ERROR,
            {"providers": {"claude": {"error": "request failed: rate limited: HTTP 429"}}},
        )

    monkeypatch.setattr(ai_usage, "fetch_usage", fake_fetch)
    monkeypatch.setattr(ai_usage, "RETRY_BACKOFF_SECONDS", (0,))

    fetcher = UsageFetcher()
    fetcher.refresh()
    await fetcher._task
    assert fetcher.get().status is FetchStatus.ERROR
    assert fetcher._retry_task is None
