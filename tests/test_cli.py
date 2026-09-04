"""Tests for command-line-only behavior."""

from __future__ import annotations

from ulanzi_niri import ai_usage, cli


def _report() -> dict:
    return {
        "providers": {
            "claude": {
                "accounts": [
                    {
                        "email": "",
                        "active": True,
                        "limits": {
                            "five_hour": {
                                "remaining_percent": 82.5,
                                "reset_after_seconds": 3660,
                            },
                            "seven_day_fable": {
                                "remaining_percent": 40,
                                "reset_after_seconds": 90000,
                            },
                        },
                    }
                ]
            },
            "codex": {"error": "auth missing: run `codex login`"},
            "opencode-go": {
                "accounts": [
                    {
                        "email": "",
                        "active": True,
                        "limits": {
                            "rolling": {
                                "remaining_percent": 75,
                                "reset_after_seconds": 3600,
                            },
                            "monthly": {
                                "remaining_percent": 90,
                                "reset_after_seconds": 86400,
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
                                "remaining_amount": 97.32403,
                                "currency": "CNY",
                            },
                        },
                    }
                ]
            },
        }
    }


def test_usage_subcommand_parser() -> None:
    args = cli.build_parser().parse_args(["ai-usage", "--timeout", "3"])
    assert args.cmd == "ai-usage"
    assert args.timeout == 3


def test_format_usage_report() -> None:
    text, succeeded = cli._format_usage_report(_report())
    assert succeeded is True
    assert "Claude" in text
    assert "5-hour: 82.5% remaining, resets in 1h1m" in text
    assert "7-day Fable: 40% remaining, resets in 1d1h" in text
    assert "Codex: unavailable (auth missing: run `codex login`)" in text
    assert "OpenCode Go" in text
    assert "5-hour rolling: 75% remaining, resets in 1h0m" in text
    assert "Monthly: 90% remaining, resets in 1d0h" in text
    assert "Moonshot" in text
    assert "Balance: ¥97.32 available" in text


def test_usage_subcommand_prints_partial_success(monkeypatch, capsys) -> None:
    async def fake_fetch(timeout: float = 20.0) -> ai_usage.UsageFetchResult:
        assert timeout == 3
        return ai_usage.UsageFetchResult(ai_usage.FetchStatus.ERROR, _report())

    monkeypatch.setattr(ai_usage, "fetch_usage", fake_fetch)
    assert cli.main(["ai-usage", "--timeout", "3"]) == 0
    assert "82.5% remaining" in capsys.readouterr().out


def test_usage_subcommand_fails_without_provider_data(monkeypatch, capsys) -> None:
    async def fake_fetch(timeout: float = 20.0) -> ai_usage.UsageFetchResult:
        return ai_usage.UsageFetchResult(
            ai_usage.FetchStatus.ERROR,
            {
                "providers": {
                    "claude": {"error": "auth missing"},
                    "codex": {"error": "auth missing"},
                }
            },
        )

    monkeypatch.setattr(ai_usage, "fetch_usage", fake_fetch)
    assert cli.main(["ai-usage"]) == 1
    output = capsys.readouterr().out
    assert "Claude: unavailable" in output
    assert "Codex: unavailable" in output


def test_usage_subcommand_rejects_invalid_timeout(capsys) -> None:
    assert cli.main(["ai-usage", "--timeout", "0"]) == 2
    assert "timeout must be greater than zero" in capsys.readouterr().err


def test_version_subcommand(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.startswith("ulanzi-niri ")
