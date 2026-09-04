"""Tests for the plan-usage fetcher and renderer."""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image, ImageDraw

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
    "moonshot": {
        "accounts": [
            {
                "email": "",
                "active": True,
                "limits": {
                    "balance": {
                        "remaining_amount": 49.59,
                        "currency": "CNY",
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
    assert _default_label(UsageWidget(pos=1, provider="moonshot", limit="balance")) == "BAL"


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


def test_provider_error_does_not_classify_transport_failure_as_auth() -> None:
    provider = ai_usage._provider_error(
        "request to https://api.anthropic.com failed: SSL: UNEXPECTED_EOF_WHILE_READING"
    )

    assert provider == {
        "error": "request failed: request to https://api.anthropic.com failed: "
        "SSL: UNEXPECTED_EOF_WHILE_READING"
    }
    assert result_auth_denied({"providers": {"claude": provider}}) is False


def test_provider_error_classifies_missing_and_rejected_credentials() -> None:
    assert ai_usage._provider_error("claude token not found") == {
        "error": "auth missing: claude token not found"
    }
    assert ai_usage._provider_error("HTTP 401 from https://example.test") == {
        "error": "auth denied: HTTP 401 from https://example.test"
    }


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
    monkeypatch.setattr(ai_usage, "_fetch_moonshot", lambda _timeout: PROVIDERS["moonshot"])
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
    monkeypatch.setattr(ai_usage, "_fetch_moonshot", lambda _timeout: PROVIDERS["moonshot"])
    result = await ai_usage.fetch_usage()
    assert result.status is FetchStatus.ERROR
    assert result.data == {
        "providers": {
            "claude": PROVIDERS["claude"],
            "codex": error,
            "opencode-go": PROVIDERS["opencode-go"],
            "moonshot": PROVIDERS["moonshot"],
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


def test_moonshot_credential_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", " test-key ")
    monkeypatch.delenv("MOONSHOT_BASE_URL", raising=False)
    key, url, currency = ai_usage._moonshot_credential()
    assert key == "test-key"
    assert url == ai_usage._MOONSHOT_BALANCE_URL_AI
    assert currency == "USD"


def test_moonshot_credential_base_url_override(monkeypatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1/")
    key, url, currency = ai_usage._moonshot_credential()
    assert key == "test-key"
    assert url == "https://api.moonshot.cn/v1/users/me/balance"
    assert currency == "CNY"


def test_moonshot_credential_from_auth_file(monkeypatch, tmp_path) -> None:
    auth_dir = tmp_path / "opencode"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps(
            {
                "moonshotai": {"type": "api", "key": "intl-key"},
                "moonshotai-cn": {"type": "api", "key": "cn-key"},
            }
        )
    )
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    key, url, currency = ai_usage._moonshot_credential()
    assert key == "intl-key"
    assert url == ai_usage._MOONSHOT_BALANCE_URL_AI
    assert currency == "USD"


def test_moonshot_credential_cn_fallback(monkeypatch, tmp_path) -> None:
    auth_dir = tmp_path / "opencode"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps({"moonshotai-cn": {"type": "api", "key": "cn-key"}})
    )
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    key, url, currency = ai_usage._moonshot_credential()
    assert key == "cn-key"
    assert url == ai_usage._MOONSHOT_BALANCE_URL_CN
    assert currency == "CNY"


def test_fetch_moonshot(monkeypatch) -> None:
    monkeypatch.setattr(
        ai_usage,
        "_moonshot_credential",
        lambda: ("test-key", ai_usage._MOONSHOT_BALANCE_URL_CN, "CNY"),
    )
    monkeypatch.setattr(
        ai_usage,
        "_request_json",
        lambda *_args, **_kwargs: {
            "code": 0,
            "data": {
                "available_balance": 97.32403,
                "voucher_balance": 90.0,
                "cash_balance": 7.32403,
            },
            "scode": "0x0",
            "status": True,
        },
    )

    provider = ai_usage._fetch_moonshot(5)
    limits = provider["accounts"][0]["limits"]

    assert limits["balance"] == {"remaining_amount": 97.32403, "currency": "CNY"}


def test_fetch_moonshot_bad_response(monkeypatch) -> None:
    monkeypatch.setattr(
        ai_usage,
        "_moonshot_credential",
        lambda: ("test-key", ai_usage._MOONSHOT_BALANCE_URL_AI, "USD"),
    )
    monkeypatch.setattr(ai_usage, "_request_json", lambda *_args, **_kwargs: {"code": 10001})

    provider = ai_usage._fetch_moonshot(5)

    assert "error" in provider


def test_format_balance() -> None:
    assert ai_usage.format_balance(4.96, "USD") == "$4.96"
    assert ai_usage.format_balance(97.32403, "CNY") == "¥97.32"
    assert ai_usage.format_balance(1.5, "EUR") == "EUR 1.50"


def test_balance_color_thresholds() -> None:
    assert ai_usage._balance_color(70.0, "CNY") == ai_usage._COLOR_GREEN
    assert ai_usage._balance_color(36.0, "CNY") == ai_usage._COLOR_YELLOW
    assert ai_usage._balance_color(35.99, "CNY") == ai_usage._COLOR_RED
    assert ai_usage._balance_color(12.0, "USD") == ai_usage._COLOR_GREEN
    assert ai_usage._balance_color(6.0, "USD") == ai_usage._COLOR_YELLOW
    assert ai_usage._balance_color(5.99, "USD") == ai_usage._COLOR_RED


def test_resolve_limit_balance() -> None:
    info = resolve_limit(PROVIDERS, "moonshot", "", "balance")
    assert info is not None
    assert info.remaining_amount == 49.59
    assert info.currency == "CNY"
    assert info.remaining_percent is None


def test_render_widget_balance(monkeypatch) -> None:
    drawn = []
    monkeypatch.setattr(
        ai_usage, "_draw_fit", lambda _draw, _center, text, *_args, **_kwargs: drawn.append(text)
    )
    bottom = []
    monkeypatch.setattr(
        ai_usage,
        "_draw_fit_bottom",
        lambda _draw, _cx, _bottom, text, *_args, **_kwargs: bottom.append(text),
    )
    widget = UsageWidget(pos=1, provider="moonshot", account="", limit="balance")

    render_widget(widget, PROVIDERS)

    assert "¥49" in drawn
    assert bottom == [".59"]


def test_balance_parts() -> None:
    assert ai_usage._balance_parts(87.32403, "CNY") == ("¥87", ".32")
    assert ai_usage._balance_parts(123.45, "USD") == ("$123", ".45")
    assert ai_usage._balance_parts(5.999, "USD") == ("$6", ".00")
    assert ai_usage._balance_parts(-2.5, "USD") == ("$0", ".00")
    assert ai_usage._balance_parts(12345.0, "CNY") == ("¥12K", ".345")
    assert ai_usage._balance_parts(12346.0, "CNY") == ("¥12K", ".346")
    assert ai_usage._balance_parts(123456.0, "USD") == ("$123K", ".456")
    assert ai_usage._balance_parts(1500.0, "USD") == ("$1K", ".500")
    assert ai_usage._balance_parts(12345678.0, "USD") == ("$12M", ".345")
    assert ai_usage._balance_parts(45_000_000_000.0, "USD") == ("$45B", ".000")
    assert ai_usage._balance_parts(999500.0, "USD") == ("$999K", ".500")
    assert ai_usage._balance_parts(12.5, "EUR") == ("EUR 12", ".50")


def test_draw_fit_reference_keeps_short_text_at_reference_size(monkeypatch) -> None:
    img = Image.new("RGB", (196, 196))
    draw = ImageDraw.Draw(img)
    sizes = {}
    monkeypatch.setattr(
        ai_usage,
        "_draw_centered",
        lambda _draw, _center, text, font, _fill: sizes.update({text: font.size}),
    )
    reference = "$00M"
    ai_usage._draw_fit(draw, (98, 98), "$12", 56, (0, 0, 0), 156, reference=reference)
    ai_usage._draw_fit(draw, (98, 98), "$123", 56, (0, 0, 0), 156, reference=reference)
    ai_usage._draw_fit(draw, (98, 98), "$12K", 56, (0, 0, 0), 156, reference=reference)
    ai_usage._draw_fit(draw, (98, 98), "$123K", 56, (0, 0, 0), 156, reference=reference)

    assert sizes["$12"] == sizes["$123"] == sizes["$12K"]
    assert sizes["$123K"] < sizes["$123"]


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


def test_request_refreshes_and_retries_once_after_401(monkeypatch) -> None:
    requested_tokens = []
    refreshed = []

    def request(_url, token, _timeout, **_kwargs):
        requested_tokens.append(token)
        if len(requested_tokens) == 1:
            raise ai_usage._HTTPStatusError(401, "HTTP 401")
        return {"ok": True}

    monkeypatch.setattr(ai_usage, "_request_json", request)

    result = ai_usage._request_json_retry_unauthorized(
        "https://example.test/usage",
        "old-access",
        5,
        lambda: refreshed.append(True) or "new-access",
    )

    assert result == {"ok": True}
    assert requested_tokens == ["old-access", "new-access"]
    assert refreshed == [True]


def test_request_does_not_refresh_or_retry_non_401(monkeypatch) -> None:
    refreshed = []

    def request(*_args, **_kwargs):
        raise ai_usage._HTTPStatusError(403, "HTTP 403")

    monkeypatch.setattr(ai_usage, "_request_json", request)

    with pytest.raises(ai_usage._HTTPStatusError, match="HTTP 403"):
        ai_usage._request_json_retry_unauthorized(
            "https://example.test/usage",
            "old-access",
            5,
            lambda: refreshed.append(True) or "new-access",
        )

    assert refreshed == []


def test_request_surfaces_second_401_without_another_refresh(monkeypatch) -> None:
    attempts = []
    refreshed = []

    def request(_url, token, _timeout, **_kwargs):
        attempts.append(token)
        raise ai_usage._HTTPStatusError(401, "HTTP 401")

    monkeypatch.setattr(ai_usage, "_request_json", request)

    with pytest.raises(ai_usage._HTTPStatusError, match="HTTP 401"):
        ai_usage._request_json_retry_unauthorized(
            "https://example.test/usage",
            "old-access",
            5,
            lambda: refreshed.append(True) or "new-access",
        )

    assert attempts == ["old-access", "new-access"]
    assert refreshed == [True]


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
    monkeypatch.setattr(ai_usage, "_fetch_moonshot", timed_out)
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
