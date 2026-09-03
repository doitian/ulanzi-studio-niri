"""Fetch and render ``aistat`` usage for the D200X LCD buttons.

Reads remaining-plan-usage data from the `aistat` CLI (see
https://github.com/drogers0/aistat), which reports the remaining percentage
and reset time for the usage windows of each Claude/Codex account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from io import BytesIO

from PIL import Image, ImageDraw

from .config import AistatWidget
from .icons import _font as load_font
from .icons import _load_icon_image, resolve_icon_path
from .protocol.ulanzi_d200x import STD_ICON

log = logging.getLogger(__name__)

AISTAT_REFRESH_SECONDS = 30 * 60

RETRY_BACKOFF_SECONDS = (30, 60, 120, 300, 600)

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

    Failed fetches are retried with a capped exponential backoff
    (``RETRY_BACKOFF_SECONDS``) instead of waiting for the next external
    refresh, so a transient failure recovers on its own. Auth failures (e.g. an
    expired Claude token needing ``claude /login``) are cached as-is and never
    retried — the renderer surfaces them as ``401``.
    """

    def __init__(self, *, ttl: float = AISTAT_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._result: UsageFetchResult | None = None
        self._fetched_at: float | None = None
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._retry_attempt = 0
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
        result = await fetch_usage()
        self._result = result
        self._fetched_at = asyncio.get_running_loop().time()
        log.debug("usage fetch complete: status=%s", result.status)
        await self._notify()
        if result.status is FetchStatus.OK or result_auth_denied(result.data):
            self._retry_attempt = 0
        else:
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            return
        idx = min(self._retry_attempt, len(RETRY_BACKOFF_SECONDS) - 1)
        delay = RETRY_BACKOFF_SECONDS[idx]
        self._retry_attempt += 1
        log.info("usage fetch failed; retrying in %.0fs", delay)
        self._retry_task = asyncio.create_task(self._retry_later(delay))

    async def _retry_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._retry_task = None
        await self._run()

    async def _notify(self) -> None:
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

    The CLI prints JSON on stdout by default (``--human`` opts into text). The
    JSON is preserved even on a non-zero exit so the renderer can surface
    per-provider errors such as an expired token.
    """
    argv = ["aistat", "usage"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error("aistat binary not found on PATH; install it to show usage widgets")
        return UsageFetchResult(FetchStatus.ERROR)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error("aistat timed out after %.0fs", timeout)
        return UsageFetchResult(FetchStatus.TIMEOUT)

    detail = err.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        log.warning("aistat exited %d%s", proc.returncode, f": {detail}" if detail else "")
    try:
        payload = json.loads(out.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("aistat returned non-JSON output")
        return UsageFetchResult(FetchStatus.ERROR)
    status = FetchStatus.OK if proc.returncode == 0 else FetchStatus.ERROR
    return UsageFetchResult(status, payload)


_AUTH_ERROR_MARKERS = (
    "auth denied",
    "auth missing",
    "credential expired",
    "token not found",
    "tokens revoked",
    "token_invalidated",
    "token_revoked",
)


def _is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


def resolve_account(providers: dict, provider: str, account: str) -> dict | None:
    """Resolve a provider's account dict from aistat's ``providers`` object.

    ``account`` matches an entry's email; when empty the provider's ``active``
    account is used (falling back to the first entry).
    """
    p = providers.get(provider)
    if not isinstance(p, dict):
        return None
    accounts = p.get("accounts")
    if not isinstance(accounts, list):
        return None
    if account:
        for a in accounts:
            if isinstance(a, dict) and a.get("email") == account:
                return a
        return None
    for a in accounts:
        if isinstance(a, dict) and a.get("active"):
            return a
    if accounts and isinstance(accounts[0], dict):
        return accounts[0]
    return None


def resolve_limit(
    providers: dict, provider: str, account: str, limit: str
) -> AistatLimit | None:
    """Pick a limit out of aistat's ``providers`` object for a provider/account."""
    target = resolve_account(providers, provider, account)
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


def resolve_auth_error(providers: dict, provider: str, account: str) -> str | None:
    """Return the auth error message for a provider/account, else ``None``.

    Checks the provider-level ``error`` first (e.g. ``auth missing``), then the
    resolved account's ``error`` (e.g. an expired token needing re-login).
    """
    p = providers.get(provider)
    if isinstance(p, dict):
        err = p.get("error")
        if isinstance(err, str) and err and _is_auth_error(err):
            return err
    target = resolve_account(providers, provider, account)
    if target is not None:
        err = target.get("error")
        if isinstance(err, str) and err and _is_auth_error(err):
            return err
    return None


_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def resolve_http_status(providers: dict, provider: str, account: str) -> int | None:
    """Return the HTTP status code from a provider/account error, else ``None``.

    aistat embeds failed status codes in its error text (e.g. ``"HTTP 401"``,
    ``"HTTP 429"``); this surfaces the code for a single widget's provider and
    account so one failing provider does not affect the others.
    """
    p = providers.get(provider)
    if isinstance(p, dict):
        err = p.get("error")
        if isinstance(err, str):
            m = _HTTP_STATUS_RE.search(err)
            if m is not None:
                return int(m.group(1))
    target = resolve_account(providers, provider, account)
    if target is not None:
        err = target.get("error")
        if isinstance(err, str):
            m = _HTTP_STATUS_RE.search(err)
            if m is not None:
                return int(m.group(1))
    return None


def result_auth_denied(data: dict | None) -> bool:
    """True when any provider/account in an aistat result reports an auth error."""
    if not isinstance(data, dict):
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for p in providers.values():
        if not isinstance(p, dict):
            continue
        err = p.get("error")
        if isinstance(err, str) and err and _is_auth_error(err):
            return True
        accounts = p.get("accounts")
        if isinstance(accounts, list):
            for a in accounts:
                if isinstance(a, dict):
                    err = a.get("error")
                    if isinstance(err, str) and err and _is_auth_error(err):
                        return True
    return False


def format_reset(seconds: float) -> str:
    """Humanize a reset countdown with two units, e.g. ``"1h2m"``, ``"3d4h"``."""
    s = int(seconds)
    if s <= 0:
        return "now"
    if s < 60:
        return "<1m"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _default_label(widget: AistatWidget) -> str:
    return {
        "five_hour": "5H",
        "seven_day": "7D",
        "seven_day_fable": "FABLE",
    }[widget.limit]


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


def _load_corner_icon(name: str, size: int) -> Image.Image | None:
    """Resolve and scale an app icon to fit in a ``size``x``size`` corner."""
    path = resolve_icon_path(name)
    if path is None:
        log.warning("widget icon %r not found in any search path", name)
        return None
    try:
        icon = _load_icon_image(str(path), size, "#FFFFFF")
    except (OSError, ValueError, ImportError) as exc:
        log.warning("failed to load widget icon %s: %s", path, exc)
        return None
    scale = min(size / icon.width, size / icon.height)
    w = max(1, round(icon.width * scale))
    h = max(1, round(icon.height * scale))
    return icon.resize((w, h), Image.Resampling.LANCZOS)


_COLOR_GRAY = (160, 160, 160)
_COLOR_GREEN = (0, 200, 80)
_COLOR_YELLOW = (240, 190, 0)
_COLOR_RED = (230, 60, 50)


def render_widget(
    widget: AistatWidget,
    providers: dict,
    *,
    status: FetchStatus = FetchStatus.OK,
    size: int = WIDGET_SIZE[0],
    padding: int = 20,
) -> bytes:
    """Render a single usage widget to a square PNG (default 196x196)."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    info = resolve_limit(providers, widget.provider, widget.account, widget.limit)
    http_status = resolve_http_status(providers, widget.provider, widget.account)
    auth_error = resolve_auth_error(providers, widget.provider, widget.account)
    label = widget.label or _default_label(widget)
    cx = size // 2
    max_width = size - 2 * padding

    draw.text((padding, padding - 4), label, font=load_font(28), fill=(255, 255, 255))
    if widget.icon:
        icon = _load_corner_icon(widget.icon, 40)
        if icon is not None:
            img.paste(icon, (size - padding - icon.width, padding), icon)

    if status is FetchStatus.TIMEOUT:
        pct = "TO"
        color = _COLOR_RED
    elif http_status is not None:
        pct = str(http_status)
        color = _COLOR_RED
    elif auth_error is not None:
        pct = "401"
        color = _COLOR_RED
    elif status is FetchStatus.ERROR:
        pct = "Err"
        color = _COLOR_RED
    elif info is None:
        pct = "n/a"
        color = _COLOR_GRAY
    elif info.remaining_percent >= 60:
        pct = f"{info.remaining_percent:.0f}%"
        color = _COLOR_GREEN
    elif info.remaining_percent >= 30:
        pct = f"{info.remaining_percent:.0f}%"
        color = _COLOR_YELLOW
    else:
        pct = f"{info.remaining_percent:.0f}%"
        color = _COLOR_RED
    _draw_fit(draw, (cx, size // 2), pct, 56, color, max_width)

    if info is not None:
        _draw_fit(
            draw,
            (cx, size - 28),
            format_reset(info.reset_after_seconds),
            28,
            (180, 180, 180),
            max_width,
        )

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
