"""Fetch and render ``aistat`` usage for the D200X LCD buttons.

Reads remaining-plan-usage data from the `aistat` CLI (see
https://github.com/drogers0/aistat), which reports the remaining percentage
and reset time for the usage windows of each Claude/Codex account.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from io import BytesIO

from PIL import Image, ImageDraw, ImageEnhance

from .config import AistatWidget
from .icons import _font as load_font
from .icons import _load_icon_image, resolve_icon_path
from .protocol.ulanzi_d200x import STD_ICON

log = logging.getLogger(__name__)

AISTAT_REFRESH_SECONDS = 30 * 60

WIDGET_SIZE = STD_ICON  # (196, 196)


@dataclass
class AistatLimit:
    remaining_percent: float
    resets_at: str
    reset_after_seconds: float


class FetchStatus(Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class UsageFetchResult:
    status: FetchStatus
    data: dict | None = None


AISTAT_TTL_SECONDS = 90


class UsageFetcher:
    """Serve cached aistat usage and refresh it in the background.

    ``get()`` returns the last known result immediately and never blocks;
    ``refresh()`` starts a background fetch only when the cache is older than
    ``ttl`` and no fetch is already in flight. A finished fetch invokes
    ``on_update`` so callers can repaint with fresh data.
    """

    def __init__(self, *, ttl: float = AISTAT_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._result: UsageFetchResult | None = None
        self._fetched_at: float | None = None
        self._task: asyncio.Task | None = None
        self._on_update: Callable[[], Awaitable[None]] | None = None

    def set_on_update(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_update = callback

    def get(self) -> UsageFetchResult | None:
        return self._result

    def refresh(self, *, force: bool = False) -> None:
        if self._task is not None and not self._task.done():
            return
        if not force and self._fetched_at is not None:
            age = asyncio.get_running_loop().time() - self._fetched_at
            if age < self._ttl:
                return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        self._result = await fetch_usage()
        self._fetched_at = asyncio.get_running_loop().time()
        log.debug("usage fetch complete: status=%s", self._result.status)
        callback = self._on_update
        if callback is not None:
            try:
                await callback()
            except Exception:  # noqa: BLE001
                log.exception("usage update callback failed")


async def fetch_usage(timeout: float = 20.0) -> UsageFetchResult:
    """Run ``aistat usage`` once and return its parsed JSON.

    ``--refresh`` is deliberately omitted so the CLI observes its local 90s
    TTL instead of hitting the API every time (which risks rate-limit errors).

    The CLI prints JSON on stdout by default (``--human`` opts into text).
    """
    argv = ["aistat", "usage"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.error("aistat binary not found on PATH; install it to show usage widgets")
        return UsageFetchResult(FetchStatus.ERROR)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error("aistat timed out after %.0fs", timeout)
        return UsageFetchResult(FetchStatus.TIMEOUT)
    if proc.returncode != 0:
        log.warning("aistat exited %d", proc.returncode)
        return UsageFetchResult(FetchStatus.ERROR)
    try:
        return UsageFetchResult(FetchStatus.OK, json.loads(out.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("aistat returned non-JSON output")
        return UsageFetchResult(FetchStatus.ERROR)


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
    short = {
        "five_hour": "5h",
        "seven_day": "1w",
        "seven_day_fable": "fable",
    }[widget.limit]
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


def _cover_fit(icon: Image.Image, size: int) -> Image.Image:
    """Scale ``icon`` to cover ``size``x``size``, center-cropping the overflow."""
    scale = max(size / icon.width, size / icon.height)
    w = max(size, round(icon.width * scale))
    h = max(size, round(icon.height * scale))
    icon = icon.resize((w, h), Image.Resampling.LANCZOS)
    left = (w - size) // 2
    top = (h - size) // 2
    return icon.crop((left, top, left + size, top + size))


def _load_background_icon(name: str, size: int) -> Image.Image | None:
    """Resolve, cover-fit, and dim an app icon for use as a widget background."""
    path = resolve_icon_path(name)
    if path is None:
        log.warning("widget background icon %r not found in any search path", name)
        return None
    try:
        icon = _load_icon_image(str(path), size, "#FFFFFF")
    except (OSError, ValueError, ImportError) as exc:
        log.warning("failed to load widget background icon %s: %s", path, exc)
        return None
    icon = _cover_fit(icon, size)
    alpha = icon.getchannel("A")
    dim = ImageEnhance.Brightness(icon.convert("RGB")).enhance(0.35)
    dim = Image.blend(dim, Image.new("RGB", (size, size), (0, 0, 0)), 0.4)
    dim.putalpha(alpha)
    return dim


def render_widget(
    widget: AistatWidget,
    providers: dict,
    *,
    status: FetchStatus = FetchStatus.OK,
    size: int = WIDGET_SIZE[0],
    padding: int = 12,
) -> bytes:
    """Render a single usage widget to a square PNG (default 196x196)."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    if widget.background:
        bg = _load_background_icon(widget.background, size)
        if bg is not None:
            img.paste(bg, (0, 0), bg)
    draw = ImageDraw.Draw(img)

    info = resolve_limit(providers, widget.provider, widget.account, widget.limit)
    label = widget.label or _default_label(widget)
    cx = size // 2
    max_width = size - 2 * padding

    _draw_fit(draw, (cx, 28), label, 26, (255, 255, 255), max_width)

    if status is FetchStatus.TIMEOUT:
        pct = "TO"
    elif status is FetchStatus.ERROR:
        pct = "Err"
    elif info is None:
        pct = "n/a"
    else:
        pct = f"{info.remaining_percent:.0f}%"
    _draw_fit(draw, (cx, size // 2), pct, 48, (255, 255, 255), max_width)

    if info is not None:
        reset = f"resets {format_reset(info.reset_after_seconds)}"
        _draw_fit(draw, (cx, size - 24), reset, 18, (180, 180, 180), max_width)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
