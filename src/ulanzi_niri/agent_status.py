"""Agent status widget: coding agent session counts from agent-berth on D200X buttons.

agent-berth owns hooks, session tracking, and pruning; this module only reads
its snapshots. It runs ``agent-berth stats --json`` (override the executable
with ``ULANZI_AGENT_BERTH``), merges repeated provider rows, and shares one
reading across every widget: reads are throttled to a short interval, forced
reads have an even shorter floor, and only one command runs at a time.

A failed read keeps the last counts and reports the reason as a code
(``agent_berth_missing``, ``timeout``, ``command_failed``,
``invalid_response``), so no agent-berth output reaches the widget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from .ai_usage import _draw_fit, _draw_fit_bottom, _load_corner_icon
from .config import AgentStatusWidget
from .icons import LABEL_BOTTOM_PADDING
from .icons import _font as load_font
from .protocol.ulanzi_d200x import STD_ICON

log = logging.getLogger(__name__)

AGENT_POLL_SECONDS = 2.0
MANUAL_REFRESH_THROTTLE_SECONDS = 0.5
COMMAND_TIMEOUT_SECONDS = 5.0
STALE_AFTER_SECONDS = 30.0

# Highest priority first: the widget shows the status that most wants attention.
STATUSES = ("waiting", "running", "done", "idle")

_PROVIDER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}$")

_ERROR_CODES = frozenset({"agent_berth_missing", "timeout", "command_failed", "invalid_response"})

_STATUS_COLORS = {
    "waiting": (242, 166, 90),
    "running": (232, 207, 120),
    "done": (125, 170, 220),
    "idle": (133, 201, 149),
}
_GRAY = (160, 160, 160)
_RED = (230, 60, 50)
_DARK = (50, 50, 50)

# (center, footer, color) shown when no reading has arrived yet.
_ERRORS = {
    "agent_berth_missing": ("n/a", "NO CLI", _GRAY),
    "timeout": ("TO", "AGENT-BERTH", _RED),
    "command_failed": ("Err", "AGENT-BERTH", _RED),
    "invalid_response": ("Err", "AGENT-BERTH", _RED),
}

_DEFAULT_ICONS = {
    "all": "utilities-terminal",
    "claude": "claude-desktop",
    "codex": "chatgpt",
    "opencode": "opencode",
    "grok": "xai",
}

_BAR_HEIGHT = 10
_BAR_MARGIN = 8
_BAR_GAP = 6


def parse_stats(payload: object) -> dict[str, dict[str, int]]:
    """Validate agent-berth ``stats --json`` rows, merging repeated providers."""
    if not isinstance(payload, list):
        raise RuntimeError("invalid_response")
    providers: dict[str, dict[str, int]] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise RuntimeError("invalid_response")
        name = row.get("provider")
        if not isinstance(name, str):
            raise RuntimeError("invalid_response")
        name = name.strip().lower()
        if not _PROVIDER_NAME.fullmatch(name):
            raise RuntimeError("invalid_response")
        counts = providers.setdefault(name, dict.fromkeys(STATUSES, 0))
        for status in STATUSES:
            value = row.get(status)
            # bool is an int subclass; reject it along with floats and negatives.
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError("invalid_response")
            # agent-berth may report a provider across several rows.
            counts[status] += value
    return providers


async def read_stats() -> dict[str, dict[str, int]]:
    """Run ``agent-berth stats --json`` and return validated per-provider counts."""
    command = os.environ.get("ULANZI_AGENT_BERTH", "").strip() or "agent-berth"
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "stats",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("agent_berth_missing") from exc
    except OSError as exc:
        raise RuntimeError("command_failed") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError("timeout") from None
    if proc.returncode != 0:
        raise RuntimeError("command_failed")
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid_response") from exc
    return parse_stats(payload)


async def fetch_agent_status() -> dict[str, dict[str, int]]:
    """Fetch one agent status reading (monkeypatched in tests)."""
    return await read_stats()


# Never surface agent-berth stderr, session paths, or command lines to widgets.
def _safe_error(error: BaseException) -> str:
    message = str(error)
    return message if message in _ERROR_CODES else "command_failed"


@dataclass
class AgentStatusSnapshot:
    providers: dict[str, dict[str, int]] = field(default_factory=dict)
    fetched_at: float | None = None  # monotonic time of the last successful read
    error: str | None = None


class AgentStatusFetcher:
    """Serve cached agent-berth counts and refresh them in the background.

    ``get()`` returns the last snapshot immediately and never blocks;
    ``refresh()`` starts a background read only when the previous attempt is
    older than ``ttl`` (or ``MANUAL_REFRESH_THROTTLE_SECONDS`` when forced)
    and no read is already in flight. A finished read invokes ``on_update`` so
    callers can repaint. Failures keep the last counts in the snapshot and
    record the error code, so a transient failure does not blank the keys.
    """

    def __init__(self, *, ttl: float = AGENT_POLL_SECONDS) -> None:
        self._ttl = ttl
        self._snapshot: AgentStatusSnapshot | None = None
        self._attempted_at: float | None = None
        self._task: asyncio.Task | None = None
        self._on_update: Callable[[], Awaitable[None]] | None = None

    def set_on_update(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_update = callback

    def get(self) -> AgentStatusSnapshot | None:
        return self._snapshot

    def refresh(self, *, force: bool = False) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._attempted_at is not None:
            age = asyncio.get_running_loop().time() - self._attempted_at
            minimum_age = MANUAL_REFRESH_THROTTLE_SECONDS if force else self._ttl
            if age < minimum_age:
                return
        self._task = asyncio.create_task(self._run())

    async def refresh_and_wait(self) -> AgentStatusSnapshot | None:
        """Read fresh counts, joining an in-flight read and bypassing the throttle."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        # A disconnected or cancelled caller must not cancel the daemon's read.
        await asyncio.shield(self._task)
        return self._snapshot

    async def _run(self) -> None:
        try:
            providers = await fetch_agent_status()
        except Exception as exc:  # noqa: BLE001
            prev = self._snapshot
            snapshot = AgentStatusSnapshot(
                providers=prev.providers if prev is not None else {},
                fetched_at=prev.fetched_at if prev is not None else None,
                error=_safe_error(exc),
            )
            log.warning("agent status read failed: %s", snapshot.error)
        else:
            snapshot = AgentStatusSnapshot(
                providers=providers, fetched_at=asyncio.get_running_loop().time(), error=None
            )
        self._snapshot = snapshot
        self._attempted_at = asyncio.get_running_loop().time()
        await self._notify()

    async def _notify(self) -> None:
        callback = self._on_update
        if callback is not None:
            try:
                await callback()
            except Exception:  # noqa: BLE001
                log.exception("agent status update callback failed")


@dataclass
class AgentSummary:
    status: str
    count: int
    counts: dict[str, int]


def summarize(providers: dict, agent: str) -> AgentSummary:
    """Sum session counts for one agent (or ``all``) and pick the dominant status.

    ``all`` sums every provider agent-berth reports, including ones this
    daemon has no icon for. The dominant status is the highest-priority
    non-empty one (waiting > running > done > idle).
    """
    counts = dict.fromkeys(STATUSES, 0)
    for name, row in providers.items():
        if agent != "all" and name != agent:
            continue
        if not isinstance(row, dict):
            continue
        for status in STATUSES:
            value = row.get(status)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[status] += value
    status = next((s for s in STATUSES if counts[s] > 0), "idle")
    return AgentSummary(status, counts[status], counts)


def _draw_status_glyph(
    draw: ImageDraw.ImageDraw,
    status: str,
    x: float,
    y: float,
    size: float,
    color: tuple[int, int, int],
) -> None:
    """Draw the status glyph on a 24-unit grid: bang, play triangle, check, pause."""
    u = size / 24

    def bar(gx: float, gy: float, gw: float, gh: float) -> None:
        draw.rectangle((x + gx * u, y + gy * u, x + (gx + gw) * u, y + (gy + gh) * u), fill=color)

    if status == "waiting":
        bar(9.5, 2, 5, 13)
        bar(9.5, 18, 5, 5)
    elif status == "idle":
        bar(5, 3, 5, 18)
        bar(14, 3, 5, 18)
    elif status == "running":
        draw.polygon(
            [(x + 5 * u, y + 2 * u), (x + 21 * u, y + 12 * u), (x + 5 * u, y + 22 * u)],
            fill=color,
        )
    else:  # done
        draw.line(
            [(x + 3 * u, y + 13 * u), (x + 9.5 * u, y + 19.5 * u), (x + 21 * u, y + 4.5 * u)],
            fill=color,
            width=max(1, round(4 * u)),
            joint="curve",
        )


def _count_font(
    draw: ImageDraw.ImageDraw, text: str, max_width: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Pick a font sized for ``000`` so short counts render as large as long ones."""
    size = 64
    while size > 12 and draw.textlength("000", font=load_font(size)) > max_width:
        size -= 2
    while size > 12 and draw.textlength(text, font=load_font(size)) > max_width:
        size -= 2
    return load_font(size)


def _bar_geometry(size: int) -> tuple[int, int, int]:
    """Return (x0, y, segment width) for the four-segment bottom bar."""
    seg_w = (size - 2 * _BAR_MARGIN - (len(STATUSES) - 1) * _BAR_GAP) // len(STATUSES)
    total = len(STATUSES) * seg_w + (len(STATUSES) - 1) * _BAR_GAP
    return (size - total) // 2, size - _BAR_HEIGHT - _BAR_MARGIN, seg_w


def render_agent_widget(
    widget: AgentStatusWidget,
    snapshot: AgentStatusSnapshot | None,
    *,
    now: float | None = None,
    size: int = STD_ICON[0],
    padding: int = 20,
) -> bytes:
    """Render a single agent status widget to a square PNG (default 196x196).

    Layout: label at top left, agent icon at top right, the dominant status
    glyph beside its session count across the middle, and a bottom bar whose
    four segments light for the statuses that have sessions.
    """
    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = size // 2
    max_width = size - 2 * padding
    middle_y = round(size * 0.6)

    label = widget.label or ("AGENTS" if widget.agent == "all" else widget.agent.upper())
    icon_name = widget.icon if widget.icon is not None else _DEFAULT_ICONS.get(widget.agent)
    icon = _load_corner_icon(icon_name, 40) if icon_name else None

    font = load_font(28)
    bbox = draw.textbbox((0, 0), label, font=font)
    label_h = bbox[3] - bbox[1]
    icon_center = padding + icon.height // 2 if icon is not None else None
    label_y = icon_center - label_h // 2 - bbox[1] if icon_center is not None else padding - 4
    draw.text((padding, label_y), label, font=font, fill=(255, 255, 255))
    if icon is not None:
        img.paste(icon, (size - padding - icon.width, padding), icon)

    now = time.monotonic() if now is None else now

    if snapshot is None or snapshot.fetched_at is None:
        if snapshot is None:
            center, footer, color = "...", "", _GRAY
        else:
            center, footer, color = _ERRORS.get(
                snapshot.error or "command_failed", _ERRORS["command_failed"]
            )
        _draw_fit(draw, (cx, middle_y), center, 56, color, max_width)
        if footer:
            _draw_fit_bottom(
                draw, cx, size - LABEL_BOTTOM_PADDING, footer, 28, (180, 180, 180), max_width
            )
    else:
        stale = snapshot.error is not None or now - snapshot.fetched_at > STALE_AFTER_SECONDS
        summary = summarize(snapshot.providers, widget.agent)
        color = _GRAY if stale else _STATUS_COLORS[summary.status]
        if stale:
            small = load_font(16)
            draw.text((padding, label_y + label_h + 6), "stale", font=small, fill=_GRAY)
        # Status glyph and count fill the middle, centered together as one group.
        text = str(summary.count)
        glyph = round(size * 64 / STD_ICON[0])
        gap = 12
        count_font = _count_font(draw, text, size - 2 * padding - glyph - gap)
        text_w = draw.textlength(text, font=count_font)
        group_w = glyph + gap + text_w
        x = max(6, (size - group_w) / 2)
        _draw_status_glyph(draw, summary.status, x, middle_y - glyph / 2, glyph, color)
        # Center the count on its ink box so it lines up with the glyph.
        tbbox = draw.textbbox((0, 0), text, font=count_font)
        draw.text(
            (x + glyph + gap, middle_y - (tbbox[3] - tbbox[1]) / 2 - tbbox[1]),
            text,
            font=count_font,
            fill=color,
        )
        # Bottom bar: one segment per status, lit when that status has sessions.
        x0, bar_y, seg_w = _bar_geometry(size)
        for index, status in enumerate(STATUSES):
            lit = summary.counts[status] > 0
            fill = (_GRAY if stale else _STATUS_COLORS[status]) if lit else _DARK
            left = x0 + index * (seg_w + _BAR_GAP)
            draw.rectangle((left, bar_y, left + seg_w, bar_y + _BAR_HEIGHT), fill=fill)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
