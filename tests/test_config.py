"""Smoke tests for config parsing."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from ulanzi_niri.config import Config

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "config.toml"


def _load(text: str) -> Config:
    return Config.model_validate(tomllib.loads(text))


def test_example_config_parses() -> None:
    cfg = Config.model_validate(tomllib.loads(EXAMPLE.read_text()))
    assert {p.name for p in cfg.page} == {"main", "apps"}
    assert cfg.default_page().name == "main"


def test_pos_13_rejected_for_button() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.button]]
            pos = 13
            """
        )


def test_duplicate_pos_rejected() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.button]]
            pos = 0
            [[page.button]]
            pos = 0
            """
        )


def test_default_page_inferred() -> None:
    cfg = _load(
        """
        [[page]]
        name = "a"
        [[page]]
        name = "b"
        """
    )
    assert cfg.default_page().name == "a"


def test_duplicate_default_rejected() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "a"
            default = true
            [[page]]
            name = "b"
            default = true
            """
        )


def test_url_action_parses() -> None:
    cfg = _load(
        """
        [[page]]
        name = "x"
        [[page.button]]
        pos = 0
        on_press = { type = "url", url = "https://discord.com/channels/@me" }
        """
    )
    btn = cfg.page[0].button[0]
    assert btn.on_press is not None
    assert btn.on_press.type == "url"
    assert btn.on_press.url == "https://discord.com/channels/@me"


def test_url_action_requires_scheme() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.button]]
            pos = 0
            on_press = { type = "url", url = "discord.com" }
            """
        )


def test_page_cycle_action_parses() -> None:
    cfg = _load(
        """
        [[page]]
        name = "x"
        [[page.button]]
        pos = 14
        on_press = { type = "page", cycle = -1 }
        [[page.button]]
        pos = 15
        on_press = { type = "page", cycle = 1 }
        """
    )
    prev = cfg.page[0].button[0].on_press
    nxt = cfg.page[0].button[1].on_press
    assert prev is not None and prev.cycle == -1
    assert nxt is not None and nxt.cycle == 1


def test_page_layer_defaults() -> None:
    cfg = _load(
        """
        [[page]]
        name = "a"
        [[page]]
        name = "b"
        """
    )
    assert cfg.page[0].layer == "default"
    assert cfg.page[1].layer == "default"


def test_cycle_within_layer() -> None:
    cfg = _load(
        """
        [[page]]
        name = "main"
        layer = "home"
        [[page]]
        name = "usage"
        layer = "home"
        [[page]]
        name = "apps"
        layer = "web"
        """
    )
    assert [p.name for p in cfg.pages_in_layer("home")] == ["main", "usage"]
    assert [p.name for p in cfg.pages_in_layer("web")] == ["apps"]

    # next/prev wrap around within the layer only
    assert cfg.cycle_target("main", 1) == "usage"
    assert cfg.cycle_target("main", -1) == "usage"
    assert cfg.cycle_target("usage", 1) == "main"
    # single-page layer cycles to itself
    assert cfg.cycle_target("apps", 1) == "apps"


def test_cycle_target_missing_page() -> None:
    cfg = _load(
        """
        [[page]]
        name = "a"
        """
    )
    assert cfg.cycle_target("nope", 1) is None


def test_page_widget_parses() -> None:
    cfg = _load(
        """
        [[page]]
        name = "x"
        [[page.widget]]
        pos = 1
        provider = "codex"
        account = "me@example.com"
        limit = "five_hour"
        label = "Codex 5h"
        [[page.widget]]
        pos = 2
        provider = "claude"
        limit = "seven_day_fable"
        """
    )
    widgets = cfg.page[0].widget
    assert len(widgets) == 2
    assert widgets[0].pos == 1
    assert widgets[0].provider == "codex"
    assert widgets[0].account == "me@example.com"
    assert widgets[0].limit == "five_hour"
    assert widgets[0].label == "Codex 5h"
    assert widgets[1].provider == "claude"
    assert widgets[1].account == ""
    assert widgets[1].limit == "seven_day_fable"


def test_widget_bad_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.widget]]
            pos = 1
            provider = "gemini"
            limit = "five_hour"
            """
        )


def test_widget_fable_limit_restricted_to_claude() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.widget]]
            pos = 1
            provider = "codex"
            limit = "seven_day_fable"
            """
        )


def test_widget_bad_pos_rejected() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.widget]]
            pos = 13
            provider = "claude"
            limit = "five_hour"
            """
        )


def test_widget_overlap_button_rejected() -> None:
    with pytest.raises(ValidationError):
        _load(
            """
            [[page]]
            name = "x"
            [[page.button]]
            pos = 1
            [[page.widget]]
            pos = 1
            provider = "claude"
            limit = "five_hour"
            """
        )


def test_wide_tile_time_date_mode_parses() -> None:
    cfg = _load(
        """
        [[page]]
        name = "x"
        [page.wide_tile]
        mode = "time-date"
        """
    )
    assert cfg.page[0].wide_tile is not None
    assert cfg.page[0].wide_tile.mode == "time-date"
