"""Icon rendering for D200X LCD tiles.

Each tile is a PNG of (width x height). Standard tiles are 196x196; the wide
tile is 458x196 but is not rendered through this path (it is owned by the
small-window subsystem).
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import LabelConfig

log = logging.getLogger(__name__)


_SIZE_RE = re.compile(r"(\d+)x(\d+)")
_ICON_EXTENSIONS = (".png", ".xpm")  # SVG omitted (no cairosvg dep)


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


def _system_icon_roots() -> tuple[Path, ...]:
    return (
        Path.home() / ".local" / "share" / "icons",
        Path("/usr/share/icons"),
        Path("/usr/share/pixmaps"),
    )


def _infer_size(path: Path) -> int:
    """Infer pixel size from a freedesktop-style path component (e.g. ``256x256``).

    Returns 0 when no size can be inferred (so flat directories like
    ``/usr/share/pixmaps`` and ``scalable`` SVG dirs all rank lowest).
    """
    for part in path.parts:
        m = _SIZE_RE.fullmatch(part)
        if m:
            return int(m.group(1))
    return 0


def _walk_for_icon(root: Path, candidate_basenames: Iterable[str]) -> Path | None:
    """Walk ``root`` once, returning the highest-resolution match.

    A match is any file whose basename equals one of ``candidate_basenames``.
    Resolution is inferred from a ``NxN`` path component; ties keep the first
    seen (which gives a stable ordering across runs).
    """
    if not root.is_dir():
        return None
    targets = set(candidate_basenames)
    best: Path | None = None
    best_size = -1
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        hits = targets.intersection(filenames)
        if not hits:
            continue
        d = Path(dirpath)
        size = _infer_size(d)
        for name in hits:
            if size > best_size:
                best = d / name
                best_size = size
    return best


@functools.lru_cache(maxsize=256)
def resolve_icon_path(name: str) -> Path | None:
    """Resolve an icon name to an on-disk path.

    Search order (first match wins):

    1. ``~/.config/ulanzi-niri/icons/<name>``  (user override)
    2. ``<install>/assets/icons/<name>``        (bundled)
    3. Freedesktop roots, recursive: ``~/.local/share/icons``,
       ``/usr/share/icons``, ``/usr/share/pixmaps``.

    Names containing a ``.`` are treated as literal filenames. Bare names
    are resolved against ``<name>.png`` then ``<name>.xpm`` and the
    highest-resolution hit is returned (SVG is intentionally not supported).
    """
    for base in (_user_icons_dir(), _bundled_icons_dir()):
        p = base / name
        if p.is_file():
            return p

    if "." in name:
        candidates = (name,)
    else:
        candidates = tuple(f"{name}{ext}" for ext in _ICON_EXTENSIONS)

    for root in _system_icon_roots():
        hit = _walk_for_icon(root, candidates)
        if hit is not None:
            return hit
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
    icon_path: str | None = None
    icon_mtime: float | None = None


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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

    if req.icon_path:
        try:
            icon = Image.open(req.icon_path).convert("RGBA")
            target_h = int(req.height * (0.6 if req.show_title and req.label else 0.85))
            target_w = min(req.width - 16, int(icon.width * (target_h / icon.height)))
            icon = icon.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)
            x = (req.width - icon.width) // 2
            y = 8 if req.show_title and req.label else (req.height - icon.height) // 2
            img.paste(icon, (x, y), icon)
        except (OSError, ValueError) as exc:
            log.warning("failed to load icon %s: %s", req.icon_path, exc)

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
            y = req.height - th - 18 - bbox[1]
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
    icon_path: str | None = None
    icon_mtime: float | None = None
    if icon:
        resolved = resolve_icon_path(icon)
        if resolved is not None:
            icon_path = str(resolved)
            try:
                icon_mtime = resolved.stat().st_mtime
            except OSError:
                icon_mtime = None
        else:
            log.warning("icon %r not found in any search path", icon)
    return RenderRequest(
        label=label,
        icon=icon,
        width=width,
        height=height,
        label_color=label_cfg.color,
        align=label_cfg.align,
        font_size=label_cfg.size,
        show_title=label_cfg.show_title,
        icon_path=icon_path,
        icon_mtime=icon_mtime,
    )
