"""Icon rendering for D200X LCD tiles.

Each tile is a PNG of (width x height). Standard tiles are 196x196; the wide
tile is 458x196 but is not rendered through this path (it is owned by the
small-window subsystem).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import LabelConfig

log = logging.getLogger(__name__)


def _cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    p = Path(base) / "ulanzi-niri" / "icons" / "_generated"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _user_icons_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ulanzi-niri" / "icons"


def _bundled_icons_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "assets" / "icons"


def resolve_icon_path(name: str) -> Path | None:
    """Look up an icon by name in user + bundled icon directories."""
    for base in (_user_icons_dir(), _bundled_icons_dir()):
        p = base / name
        if p.is_file():
            return p
    return None


@dataclass(frozen=True)
class RenderRequest:
    label: str
    icon: str | None
    width: int
    height: int
    label_color: str = "FFFFFF"
    bg_color: str = "000000"
    align: str = "bottom"
    font_size: int = 28
    show_title: bool = True


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Try a few fallbacks; in headless setups DejaVuSans is almost always present
    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).is_file():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        return (255, 255, 255)
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def render(req: RenderRequest) -> bytes:
    """Render a button to PNG bytes."""
    img = Image.new("RGB", (req.width, req.height), _hex_to_rgb(req.bg_color))
    draw = ImageDraw.Draw(img)

    # Optional icon: centered horizontally, anchored above the label
    if req.icon:
        path = resolve_icon_path(req.icon)
        if path is not None:
            try:
                icon = Image.open(path).convert("RGBA")
                # Reserve about 40% of height for label; icon fills the rest
                target_h = int(req.height * (0.6 if req.show_title and req.label else 0.85))
                target_w = min(req.width - 16, int(icon.width * (target_h / icon.height)))
                icon = icon.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)
                x = (req.width - icon.width) // 2
                y = 8 if req.show_title and req.label else (req.height - icon.height) // 2
                img.paste(icon, (x, y), icon)
            except (OSError, ValueError) as exc:
                log.warning("failed to load icon %s: %s", req.icon, exc)

    # Label
    if req.show_title and req.label:
        font = _font(req.font_size)
        bbox = draw.textbbox((0, 0), req.label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (req.width - tw) // 2 - bbox[0]
        if req.align == "top":
            y = 4 - bbox[1]
        elif req.align == "middle":
            y = (req.height - th) // 2 - bbox[1]
        else:  # bottom
            y = req.height - th - 6 - bbox[1]
        draw.text((x, y), req.label, font=font, fill=_hex_to_rgb(req.label_color))

    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_cached(req: RenderRequest) -> Path:
    """Render to a cached file on disk and return the path."""
    key = hashlib.sha1(repr(req).encode()).hexdigest()[:16]
    out = _cache_root() / f"{key}.png"
    if not out.exists():
        out.write_bytes(render(req))
    return out


def request_from_button(
    label: str,
    icon: str | None,
    width: int,
    height: int,
    label_cfg: LabelConfig,
) -> RenderRequest:
    return RenderRequest(
        label=label,
        icon=icon,
        width=width,
        height=height,
        label_color=label_cfg.color,
        align=label_cfg.align,
        font_size=label_cfg.size,
        show_title=label_cfg.show_title,
    )
