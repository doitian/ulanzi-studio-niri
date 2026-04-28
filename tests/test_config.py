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
