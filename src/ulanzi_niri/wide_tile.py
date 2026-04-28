"""Wide-tile rendering / payload composition for the D200X 458x196 LCD.

The wide tile is driven by OUT_SET_SMALL_WINDOW_DATA (opcode 0x0006) using a
pipe-delimited payload. The first field is a mode integer:

    0  STATS       - "0|cpu|mem|HH:MM:SS|gpu"     (matches strmdck)
    1  CLOCK       - "1|0|0|HH:MM:SS|0"           (clock-only mode)
    2  BACKGROUND  - "2|"                         (firmware-managed background)
    3  ENCODERS    - "3|l0|v0|l1|v1|l2|v2"        (EXPERIMENTAL on D200X)

The encoders payload format is a best-guess; a runtime fallback to mode CLOCK
kicks in if the firmware misbehaves.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

from .config import EncoderEntry, WideTileEntry
from .protocol.ulanzi_d200x import SmallWindowMode, UlanziD200XDevice
from .stats import StatsSnapshot, prime_cpu_sampler

log = logging.getLogger(__name__)


# Provider callable: returns a string value to display next to an encoder label
EncoderValueProvider = Callable[[int], Awaitable[str]]


@dataclass
class WideTileState:
    config: WideTileEntry
    encoders: list[EncoderEntry]
    encoder_values: dict[int, str]
    fallback_active: bool = False


def build_clock_payload(time_format: str = "%H:%M:%S") -> str:
    return f"{int(SmallWindowMode.CLOCK)}|0|0|{datetime.now().strftime(time_format)}|0"


def build_stats_payload(snap: StatsSnapshot) -> str:
    return f"{int(SmallWindowMode.STATS)}|{snap.cpu}|{snap.mem}|{snap.time}|{snap.gpu}"


def build_background_payload() -> str:
    return f"{int(SmallWindowMode.BACKGROUND)}|"


def build_encoders_payload(entries: list[EncoderEntry], values: dict[int, str]) -> str:
    parts: list[str] = [str(int(SmallWindowMode.ENCODERS))]
    by_index = {e.index: e for e in entries}
    for i in range(3):
        e = by_index.get(i)
        label = (e.label if e else "") or f"E{i}"
        value = values.get(i, "")
        parts.extend([label, value])
    return "|".join(parts)


class WideTileWorker:
    """Periodically updates the wide tile based on its mode."""

    def __init__(
        self,
        device: UlanziD200XDevice,
        state: WideTileState,
        interval_ms: int,
        provider: Optional[EncoderValueProvider] = None,
    ) -> None:
        self._device = device
        self._state = state
        self._interval = max(0.05, interval_ms / 1000.0)
        self._provider = provider
        self._task: Optional[asyncio.Task] = None
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
            except (asyncio.TimeoutError, asyncio.CancelledError):
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
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        cfg = self._state.config
        mode = "clock" if self._state.fallback_active else cfg.mode
        if mode == "clock":
            await self._device.set_small_window(build_clock_payload(cfg.format))
        elif mode == "stats":
            await self._device.set_small_window(build_stats_payload(StatsSnapshot.sample()))
        elif mode == "background":
            await self._device.set_small_window(build_background_payload())
        elif mode == "encoders":
            if self._provider is not None:
                for e in self._state.encoders:
                    try:
                        v = await self._provider(e.index)
                        self._state.encoder_values[e.index] = v
                    except Exception:  # noqa: BLE001
                        log.debug("encoder provider %d failed", e.index, exc_info=True)
            payload = build_encoders_payload(self._state.encoders, self._state.encoder_values)
            try:
                await self._device.set_small_window(payload)
            except Exception:
                log.warning("encoders mode write failed; falling back to clock", exc_info=True)
                self._state.fallback_active = True
        else:
            log.warning("unknown wide-tile mode %r; falling back to clock", mode)
            self._state.fallback_active = True
