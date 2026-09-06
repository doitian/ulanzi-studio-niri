"""Wide-tile rendering / payload composition for the D200X 458x196 LCD.

The wide tile is driven by OUT_SET_SMALL_WINDOW_DATA (opcode 0x0006) using a
pipe-delimited payload ``mode|cpu|mem|time|gpu|format|suffix``. Mode integers
(from strmdck / companion-surface):

    0    STATS              - CPU + RAM + GPU
    1    DIAL               - analog dial clock
    2    BACKGROUND         - firmware-managed background image
    200  DATE_TIME_WEEKDAY  - digital date + time + weekday
    201  TIME_WEEKDAY       - digital time + weekday
    202  TIME_DATE          - digital time + date
    203  TIME               - digital time only
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .ai_usage import USAGE_REFRESH_SECONDS
from .config import UsageWidget, WideTileEntry
from .protocol.ulanzi_d200x import WIDE_TILE_GEOMETRY, SmallWindowMode, UlanziD200XDevice
from .stats import StatsSnapshot, prime_cpu_sampler

log = logging.getLogger(__name__)

_CLOCK_MODES: dict[str, SmallWindowMode] = {
    "clock": SmallWindowMode.TIME,
    "dial": SmallWindowMode.DIAL,
    "time-date": SmallWindowMode.TIME_DATE,
    "time-weekday": SmallWindowMode.TIME_WEEKDAY,
    "date-time-weekday": SmallWindowMode.DATE_TIME_WEEKDAY,
}


@dataclass
class WideTileState:
    config: WideTileEntry
    widgets: list[UsageWidget] = field(default_factory=list)


def render_background(config: WideTileEntry, page_index: int, page_count: int) -> bytes:
    """Render the background beneath firmware content and the layer's page position."""
    size = (WIDE_TILE_GEOMETRY.width, WIDE_TILE_GEOMETRY.height)
    image = Image.new("RGB", size, "black")
    if config.mode == "background" and config.image:
        with Image.open(Path(config.image).expanduser()) as source:
            image = ImageOps.fit(source.convert("RGB"), size)
    if page_count > 1:
        left = 12
        width, height = size[0] - 2 * left, 12
        top = size[1] - height - 4
        draw = ImageDraw.Draw(image)
        draw.rectangle((left, top, left + width - 1, top + height - 1), fill="#707070")
        start = left + page_index * width // page_count
        end = left + (page_index + 1) * width // page_count
        draw.rectangle((start, top, max(start, end - 1), top + height - 1), fill="#9974F8")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def build_clock_payload(mode: SmallWindowMode, time_format: str = "%H:%M:%S") -> str:
    now = datetime.now()
    time = now.strftime(time_format)
    date = now.strftime("%Y/%m/%d")
    weekday = now.strftime("%a")
    suffix = ""
    if mode == SmallWindowMode.DATE_TIME_WEEKDAY:
        suffix = f"{date} {weekday}"
    elif mode == SmallWindowMode.TIME_WEEKDAY:
        suffix = weekday
    elif mode == SmallWindowMode.TIME_DATE:
        suffix = date
    return f"{int(mode)}|0|0|{time}|0|24H|{suffix}"


def build_stats_payload(snap: StatsSnapshot) -> str:
    return f"{int(SmallWindowMode.STATS)}|{snap.cpu}|{snap.mem}|{snap.time}|{snap.gpu}"


def build_background_payload() -> str:
    return f"{int(SmallWindowMode.BACKGROUND)}|"


class WideTileWorker:
    """Periodically updates the wide tile and refreshes usage widgets."""

    def __init__(
        self,
        device: UlanziD200XDevice,
        state: WideTileState,
        interval_ms: int,
        *,
        refresh: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._device = device
        self._state = state
        self._interval = max(0.05, interval_ms / 1000.0)
        self._refresh = refresh
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            prime_cpu_sampler()
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="wide-tile-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    def update_state(self, state: WideTileState) -> None:
        self._state = state

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last_widget_refresh = loop.time()
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("wide-tile tick failed")
            if self._refresh is not None and self._state.widgets:
                now = loop.time()
                if now - last_widget_refresh >= USAGE_REFRESH_SECONDS:
                    last_widget_refresh = now
                    try:
                        await self._refresh()
                    except Exception:  # noqa: BLE001
                        log.exception("widget refresh failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        cfg = self._state.config
        if cfg.mode in _CLOCK_MODES:
            await self._device.set_small_window(
                build_clock_payload(_CLOCK_MODES[cfg.mode], cfg.format)
            )
        elif cfg.mode == "stats":
            await self._device.set_small_window(build_stats_payload(StatsSnapshot.sample()))
        elif cfg.mode == "background":
            await self._device.set_small_window(build_background_payload())
