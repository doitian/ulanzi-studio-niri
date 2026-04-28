"""TOML configuration model for ulanzi-niri."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .protocol.ulanzi_d200x import (
    BUTTON_GEOMETRY,
    ENCODER_COUNT,
    EXTRA_BUTTON_COUNT,
    EXTRA_BUTTON_POS_BASE,
    WIDE_TILE_POS,
)

LCD_POS_RANGE = set(BUTTON_GEOMETRY.keys())
EXTRA_POS_RANGE = set(range(EXTRA_BUTTON_POS_BASE, EXTRA_BUTTON_POS_BASE + EXTRA_BUTTON_COUNT))
BUTTON_POS_RANGE = LCD_POS_RANGE | EXTRA_POS_RANGE


# ---------------------------------------------------------------------------- actions
class _ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoopAction(_ActionBase):
    type: Literal["noop"]


class NiriAction(_ActionBase):
    type: Literal["niri"]
    action: str
    args: list[str] = Field(default_factory=list)
    unsafe_raw: bool = False


class ExecAction(_ActionBase):
    type: Literal["exec"]
    cmd: str | list[str]
    shell: bool = False
    env: dict[str, str] = Field(default_factory=dict)


class UrlAction(_ActionBase):
    type: Literal["url"]
    url: str
    opener: str | None = None  # override; defaults to xdg-open

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        if "://" not in v and not v.startswith("mailto:"):
            raise ValueError(f"url {v!r} must include a scheme (e.g. https://)")
        return v


class MediaAction(_ActionBase):
    type: Literal["media"]
    cmd: Literal[
        "play-pause", "play", "pause", "next", "prev", "stop",
        "vol-up", "vol-down", "mute",
    ]
    player: str | None = None
    step: int = 5  # percent step for volume


class ScreenshotAction(_ActionBase):
    type: Literal["screenshot"]
    target: Literal["full", "region", "window"] = "region"
    output_dir: str | None = None


class KeysAction(_ActionBase):
    type: Literal["keys"]
    keys: str
    backend: Literal["auto", "wtype", "ydotool"] = "auto"


class PageAction(_ActionBase):
    type: Literal["page"]
    goto: str | None = None
    back: bool = False
    toggle: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> PageAction:
        if not (self.goto or self.back or self.toggle):
            raise ValueError("page action requires one of: goto, back=true, toggle")
        return self


class BrightnessAction(_ActionBase):
    type: Literal["brightness"]
    delta: int | None = None
    set: int | None = Field(default=None, alias="set")

    @model_validator(mode="after")
    def _validate(self) -> BrightnessAction:
        if (self.delta is None) == (self.set is None):
            raise ValueError("brightness action requires exactly one of delta or set")
        return self


class SmallWindowAction(_ActionBase):
    type: Literal["small_window"]
    mode: Literal["clock", "stats", "background"]


Action = Annotated[
    NoopAction | NiriAction | ExecAction | UrlAction | MediaAction | ScreenshotAction | KeysAction | PageAction | BrightnessAction | SmallWindowAction,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------- entries
class ButtonEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pos: int
    label: str = ""
    icon: str | None = None
    on_press: Action | None = None
    on_release: Action | None = None
    on_long_press: Action | None = None
    long_press_ms: int = 500

    @field_validator("pos")
    @classmethod
    def _validate_pos(cls, v: int) -> int:
        if v == WIDE_TILE_POS:
            raise ValueError(
                f"pos {WIDE_TILE_POS} is the wide tile; configure it under [[page.wide_tile]] instead"
            )
        if v not in BUTTON_POS_RANGE:
            raise ValueError(
                f"pos {v} out of range; valid: LCD {sorted(LCD_POS_RANGE)}, plain {sorted(EXTRA_POS_RANGE)}"
            )
        return v


class EncoderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    label: str = ""
    on_press: Action | None = None
    on_rotate_cw: Action | None = None
    on_rotate_ccw: Action | None = None

    @field_validator("index")
    @classmethod
    def _validate_index(cls, v: int) -> int:
        if not (0 <= v < ENCODER_COUNT):
            raise ValueError(f"encoder index must be 0..{ENCODER_COUNT - 1}")
        return v


class WideTileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["clock", "stats", "background"] = "clock"
    format: str = "%H:%M:%S"
    image: str | None = None  # for mode=background
    on_press: Action | None = None

    @model_validator(mode="after")
    def _check_background_image(self) -> WideTileEntry:
        if self.mode == "background" and not self.image:
            raise ValueError("wide_tile mode 'background' requires `image` path")
        return self


class PageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    default: bool = False
    button: list[ButtonEntry] = Field(default_factory=list)
    encoder: list[EncoderEntry] = Field(default_factory=list)
    wide_tile: WideTileEntry | None = None

    @field_validator("wide_tile", mode="before")
    @classmethod
    def _unwrap_wide_tile(cls, v: Any) -> Any:
        # Allow either [page.wide_tile] (table) or [[page.wide_tile]] (single-element array)
        if isinstance(v, list):
            if len(v) == 0:
                return None
            if len(v) > 1:
                raise ValueError("at most one [[page.wide_tile]] per page")
            return v[0]
        return v

    @model_validator(mode="after")
    def _no_dupe_pos(self) -> PageConfig:
        seen: set[int] = set()
        for b in self.button:
            if b.pos in seen:
                raise ValueError(f"page {self.name!r}: duplicate button pos {b.pos}")
            seen.add(b.pos)
        seen_e: set[int] = set()
        for e in self.encoder:
            if e.index in seen_e:
                raise ValueError(f"page {self.name!r}: duplicate encoder index {e.index}")
            seen_e.add(e.index)
        return self


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brightness: int = Field(default=70, ge=0, le=100)
    stats_interval_ms: int = Field(default=1000, ge=100)
    encoder_coalesce_ms: int = Field(default=50, ge=0)


class LabelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    align: Literal["top", "middle", "bottom"] = "bottom"
    color: str = "FFFFFF"
    font_name: str = "Roboto"
    show_title: bool = True
    size: int = 28
    weight: int = 80


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: DeviceConfig = Field(default_factory=DeviceConfig)
    label: LabelConfig = Field(default_factory=LabelConfig)
    page: list[PageConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_default_page(self) -> Config:
        if not self.page:
            raise ValueError("config must define at least one [[page]]")
        defaults = [p for p in self.page if p.default]
        if len(defaults) > 1:
            raise ValueError("only one page may have default = true")
        if not defaults:
            self.page[0].default = True
        names = [p.name for p in self.page]
        if len(names) != len(set(names)):
            raise ValueError("page names must be unique")
        return self

    def default_page(self) -> PageConfig:
        for p in self.page:
            if p.default:
                return p
        return self.page[0]

    def get_page(self, name: str) -> PageConfig | None:
        for p in self.page:
            if p.name == name:
                return p
        return None


# ---------------------------------------------------------------------------- loading
def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ulanzi-niri" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    p = path or default_config_path()
    with open(p, "rb") as fp:
        data: dict[str, Any] = tomllib.load(fp)
    return Config.model_validate(data)
