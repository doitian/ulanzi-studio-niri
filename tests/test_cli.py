"""Tests for command-line-only behavior."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

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
    assert args.json is False


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
    def fake_read(timeout: float, *, refresh: bool) -> ai_usage.UsageFetchResult:
        assert timeout == 3
        assert refresh is False
        return ai_usage.UsageFetchResult(ai_usage.FetchStatus.ERROR, _report())

    monkeypatch.setattr(cli, "_read_cached_usage", fake_read)
    assert cli.main(["ai-usage", "--timeout", "3"]) == 0
    assert "82.5% remaining" in capsys.readouterr().out


def test_usage_subcommand_fails_without_provider_data(monkeypatch, capsys) -> None:
    def fake_read(timeout: float, *, refresh: bool) -> ai_usage.UsageFetchResult:
        return ai_usage.UsageFetchResult(
            ai_usage.FetchStatus.ERROR,
            {
                "providers": {
                    "claude": {"error": "auth missing"},
                    "codex": {"error": "auth missing"},
                }
            },
        )

    monkeypatch.setattr(cli, "_read_cached_usage", fake_read)
    assert cli.main(["ai-usage"]) == 1
    output = capsys.readouterr().out
    assert "Claude: unavailable" in output
    assert "Codex: unavailable" in output


@pytest.mark.parametrize(
    ("status", "data", "exit_code"),
    [
        (ai_usage.FetchStatus.OK, {"providers": {"claude": _report()["providers"]["claude"]}}, 0),
        (ai_usage.FetchStatus.ERROR, _report(), 0),
        (ai_usage.FetchStatus.ERROR, {"providers": {"claude": {"error": "auth missing"}}}, 1),
        (ai_usage.FetchStatus.TIMEOUT, None, 1),
    ],
)
def test_usage_subcommand_json(monkeypatch, capsys, status, data, exit_code) -> None:
    def fake_read(timeout: float, *, refresh: bool) -> ai_usage.UsageFetchResult:
        assert timeout == 3
        return ai_usage.UsageFetchResult(status, data)

    monkeypatch.setattr(cli, "_read_cached_usage", fake_read)
    assert cli.main(["ai-usage", "--json", "--timeout", "3"]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out) == data
    assert captured.err == ""


def test_usage_subcommand_rejects_invalid_timeout(capsys) -> None:
    assert cli.main(["ai-usage", "--timeout", "0"]) == 2
    assert "timeout must be greater than zero" in capsys.readouterr().err


@pytest.mark.parametrize("extra,expected_timeout", [([], 30.0), (["--timeout", "5"], 5.0)])
def test_usage_refresh_wait_timeout(monkeypatch, capsys, extra, expected_timeout) -> None:
    def fake_read(timeout, *, refresh):
        assert timeout == expected_timeout
        assert refresh is True
        return ai_usage.UsageFetchResult(ai_usage.FetchStatus.OK, _report())

    monkeypatch.setattr(cli, "_read_cached_usage", fake_read)
    assert cli.main(["ai-usage", "--refresh", "--json", *extra]) == 0
    assert json.loads(capsys.readouterr().out) == _report()


def test_usage_without_daemon_does_not_fetch(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    async def unexpected_fetch(*args, **kwargs):
        raise AssertionError("CLI must not fetch provider data")

    monkeypatch.setattr(ai_usage, "fetch_usage", unexpected_fetch)
    assert cli.main(["ai-usage", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) is None
    assert captured.err == "driver not running\n"


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ("ERR unknown", "restart the daemon to enable cached AI usage"),
        ("OK broken", "invalid usage response from daemon"),
        ('OK {"status": "invalid", "data": null}', "invalid usage response from daemon"),
        ('OK {"status": "ok", "data": []}', "invalid usage response from daemon"),
    ],
)
def test_usage_invalid_daemon_reply(monkeypatch, tmp_path, capsys, reply, message) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    thread = _serve_one_reply(tmp_path / "ulanzi-niri.sock", reply)
    assert cli.main(["ai-usage", "--json"]) == 1
    thread.join(timeout=2)
    captured = capsys.readouterr()
    assert json.loads(captured.out) is None
    assert captured.err.strip() == message


def test_version_subcommand(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.startswith("ulanzi-niri ")


def test_goto_parser_accepts_page() -> None:
    args = cli.build_parser().parse_args(["control", "goto", "apps"])
    assert args.cmd == "control"
    assert args.control_cmd == "goto"
    assert args.page == "apps"


def test_refresh_ai_usage_control(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    thread = _serve_one_reply(
        tmp_path / "ulanzi-niri.sock", "OK refresh-requested", expected="refresh-ai-usage"
    )
    assert cli.main(["control", "refresh-ai-usage"]) == 0
    thread.join(timeout=2)
    assert capsys.readouterr().out == "refresh-requested\n"


def test_goto_without_page_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["control", "goto"])
    assert exc.value.code == 2


def test_next_page_without_socket(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert cli.main(["control", "next-page"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "driver not running\n"


def _serve_one_reply(path: Path, reply: str, *, expected: str | None = None) -> threading.Thread:
    ready = threading.Event()

    def run() -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        request = conn.recv(256)
        if expected is not None:
            assert request == (expected + "\n").encode()
        conn.sendall((reply + "\n").encode())
        conn.close()
        srv.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    return thread


def test_goto_prints_ok_page_name(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = tmp_path / "ulanzi-niri.sock"
    thread = _serve_one_reply(path, "OK apps")
    assert cli.main(["control", "goto", "apps"]) == 0
    thread.join(timeout=2)
    assert capsys.readouterr().out == "apps\n"


def test_goto_unknown_page(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = tmp_path / "ulanzi-niri.sock"
    thread = _serve_one_reply(path, "ERR no-such-page")
    assert cli.main(["control", "goto", "nope"]) == 1
    thread.join(timeout=2)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "no such page: nope\n"


def test_back_stay_put_prints_name(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = tmp_path / "ulanzi-niri.sock"
    thread = _serve_one_reply(path, "OK first")
    assert cli.main(["control", "back"]) == 0
    thread.join(timeout=2)
    assert capsys.readouterr().out == "first\n"


def test_page_verbs_do_not_open_hid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def boom() -> None:
        raise AssertionError("HID opened")

    monkeypatch.setattr(cli, "open_device", boom)
    assert cli.main(["control", "next-page"]) == 1
