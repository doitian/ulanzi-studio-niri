"""Fetch and render ``aistat`` usage for the D200X LCD buttons.

Reads remaining-plan-usage data from the `aistat` CLI (see
https://github.com/drogers0/aistat), which reports the remaining percentage
and reset time for the 5-hour and 7-day windows of each Claude/Codex account.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw

from .config import AistatWidget
from .icons import _font as load_font
from .protocol.ulanzi_d200x import STD_ICON

log = logging.getLogger(__name__)

AISTAT_REFRESH_SECONDS = 30 * 60

WIDGET_SIZE = STD_ICON  # (196, 196)


@dataclass
class AistatLimit:
    remaining_percent: float
    resets_at: str
    reset_after_seconds: float


async def fetch_usage(timeout: float = 20.0) -> dict | None:
    """Run ``aistat usage --refresh`` once and return its parsed JSON.

    The CLI prints JSON on stdout by default (``--human`` opts into text).
    """
    argv = ["aistat", "usage", "--refresh"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.error("aistat binary not found on PATH; install it to show usage widgets")
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        log.error("aistat timed out after %.0fs", timeout)
        return None
    if proc.returncode != 0:
        log.warning("aistat exited %d", proc.returncode)
        return None
    try:
        return json.loads(out.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("aistat returned non-JSON output")
        return None


def resolve_limit(
    providers: dict, provider: str, account: str, limit: str
) -> AistatLimit | None:
    """Pick a limit out of aistat's ``providers`` object.

    ``account`` matches an entry's email; when empty the provider's ``active``
    account is used (falling back to the first entry).
    """
    p = providers.get(provider)
    if not isinstance(p, dict):
        return None
    accounts = p.get("accounts")
    if not isinstance(accounts, list):
        return None
    target: dict | None = None
    if account:
        for a in accounts:
            if isinstance(a, dict) and a.get("email") == account:
                target = a
                break
    else:
        for a in accounts:
            if isinstance(a, dict) and a.get("active"):
                target = a
                break
        if target is None and accounts and isinstance(accounts[0], dict):
            target = accounts[0]
    if target is None:
        return None
    limits = target.get("limits")
    if not isinstance(limits, dict):
        return None
    lim = limits.get(limit)
    if not isinstance(lim, dict):
        return None
    return AistatLimit(
        remaining_percent=float(lim.get("remaining_percent", 0.0)),
        resets_at=str(lim.get("resets_at", "")),
        reset_after_seconds=float(lim.get("reset_after_seconds", 0.0)),
    )


def format_reset(seconds: float) -> str:
    """Humanize a reset countdown to its largest unit, e.g. ``"2d"``, ``"4h"``, ``"now"``."""
    s = int(seconds)
    if s <= 0:
        return "now"
    if s < 60:
        return "<1m"
    days, rem = divmod(s, 86400)
    if days:
        return f"{days}d"
    hours, rem = divmod(rem, 3600)
    if hours:
        return f"{hours}h"
    return f"{rem // 60}m"


def _default_label(widget: AistatWidget) -> str:
    prefix = "cl" if widget.provider == "claude" else "cx"
    short = "5h" if widget.limit == "five_hour" else "1w"
    return f"{prefix}{short}"


def _draw_centered(
    draw: ImageDraw.ImageDraw, center: tuple[int, int], text: str, font, fill
) -> None:
    """Draw ``text`` centered (horizontally and vertically) on ``center``."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(
        (center[0] - w // 2 - bbox[0], center[1] - h // 2 - bbox[1]),
        text,
        font=font,
        fill=fill,
    )


def _draw_fit(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    text: str,
    font_size: int,
    fill: tuple[int, int, int],
    max_width: int,
    *,
    min_size: int = 10,
) -> None:
    """Center ``text`` at ``center``, shrinking the font to fit ``max_width``."""
    size = font_size
    while size >= min_size:
        font = load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            _draw_centered(draw, center, text, font, fill)
            return
        size -= 2
    _draw_centered(draw, center, text, load_font(min_size), fill)


def render_widget(
    widget: AistatWidget,
    providers: dict,
    *,
    size: int = WIDGET_SIZE[0],
    padding: int = 12,
) -> bytes:
    """Render a single usage widget to a square PNG (default 196x196)."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    info = resolve_limit(providers, widget.provider, widget.account, widget.limit)
    label = widget.label or _default_label(widget)
    cx = size // 2
    max_width = size - 2 * padding

    _draw_fit(draw, (cx, 28), label, 26, (255, 255, 255), max_width)

    pct = "n/a" if info is None else f"{info.remaining_percent:.0f}%"
    _draw_fit(draw, (cx, size // 2), pct, 48, (255, 255, 255), max_width)

    if info is not None:
        reset = f"resets {format_reset(info.reset_after_seconds)}"
        _draw_fit(draw, (cx, size - 24), reset, 18, (180, 180, 180), max_width)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
