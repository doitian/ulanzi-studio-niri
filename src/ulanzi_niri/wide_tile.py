"""Wide-tile rendering / payload composition for the D200X 458x196 LCD.

The wide tile is driven by OUT_SET_SMALL_WINDOW_DATA (opcode 0x0006) using a
pipe-delimited payload. The first field is a mode integer:

    0  STATS       - "0|cpu|mem|HH:MM:SS|gpu"     (matches strmdck)
    1  CLOCK       - "1|0|0|HH:MM:SS|0"           (clock-only mode)
    2  BACKGROUND  - "2|"                         (firmware-managed background)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from .config import WideTileEntry
from .protocol.ulanzi_d200x import SmallWindowMode, UlanziD200XDevice
from .stats import StatsSnapshot, prime_cpu_sampler

log = logging.getLogger(__name__)


@dataclass
class WideTileState:
    config: WideTileEntry


def build_clock_payload(time_format: str = "%H:%M:%S") -> str:
    return f"{int(SmallWindowMode.CLOCK)}|0|0|{datetime.now().strftime(time_format)}|0"


def build_stats_payload(snap: StatsSnapshot) -> str:
    return f"{int(SmallWindowMode.STATS)}|{snap.cpu}|{snap.mem}|{snap.time}|{snap.gpu}"


def build_background_payload() -> str:
    return f"{int(SmallWindowMode.BACKGROUND)}|"


class WideTileWorker:
    """Periodically updates the wide tile based on its mode."""

    def __init__(
        self,
        device: UlanziD200XDevice,
        state: WideTileState,
        interval_ms: int,
    ) -> None:
        self._device = device
        self._state = state
        self._interval = max(0.05, interval_ms / 1000.0)
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
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("wide-tile tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        cfg = self._state.config
        if cfg.mode == "clock":
            await self._device.set_small_window(build_clock_payload(cfg.format))
        elif cfg.mode == "stats":
            await self._device.set_small_window(build_stats_payload(StatsSnapshot.sample()))
        elif cfg.mode == "background":
            await self._device.set_small_window(build_background_payload())
