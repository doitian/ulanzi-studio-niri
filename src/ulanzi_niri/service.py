"""Async service: glues the device, config, pages, and actions together."""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

from watchfiles import awatch

from .actions import ActionContext, dispatch
from .config import (
    BrightnessProvider,
    Config,
    ExecProvider,
    StaticProvider,
    WideTileEntry,
    WpctlProvider,
    load_config,
)
from .pages import PageSet
from .protocol.device import DeckEvent, DeckEventKind
from .protocol.manager import open_device
from .protocol.ulanzi_d200x import (
    WIDE_TILE_POS,
    UlanziD200XDevice,
)
from .stats import prime_cpu_sampler
from .wide_tile import WideTileState, WideTileWorker
from .zip_builder import build_buttons_zip

log = logging.getLogger(__name__)


@dataclass
class _PressState:
    pressed_at: float
    long_press_fired: bool = False
    long_press_task: asyncio.Task | None = None


class Service:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._cfg: Config = load_config(config_path)
        self._pages = PageSet(self._cfg)
        self._device: UlanziD200XDevice | None = None
        self._wide: WideTileWorker | None = None
        self._stop = asyncio.Event()
        self._press_state: dict[int, _PressState] = {}
        self._encoder_accum: dict[int, int] = {}
        self._encoder_flush_task: dict[int, asyncio.Task] = {}
        self._brightness: int = self._cfg.device.brightness

    # ------------------------------------------------------------------ public lifecycle
    async def run(self) -> None:
        prime_cpu_sampler()
        reload_task = asyncio.create_task(self._watch_config(), name="config-watcher")
        try:
            while not self._stop.is_set():
                await self._connect_and_serve()
                if self._stop.is_set():
                    break
                log.info("device disconnected; waiting 2s before reconnecting")
                await asyncio.sleep(2.0)
        finally:
            reload_task.cancel()
            if self._wide is not None:
                await self._wide.stop()
            if self._device is not None:
                self._device.close()

    async def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ device cycle
    async def _connect_and_serve(self) -> None:
        log.info("waiting for device")
        device = await self._wait_for_device_async()
        if device is None:
            return  # stop requested
        self._device = device
        log.info("device opened: %s", device.DECK_NAME)
        try:
            await self._initial_push()
            await self._event_loop()
        finally:
            if self._wide is not None:
                await self._wide.stop()
                self._wide = None
            device.close()
            self._device = None

    async def _wait_for_device_async(self, poll_interval: float = 1.0) -> UlanziD200XDevice | None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            dev = await loop.run_in_executor(None, open_device)
            if dev is not None:
                return dev
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_interval)
                return None  # stop fired
            except TimeoutError:
                continue
        return None

    async def _initial_push(self) -> None:
        assert self._device is not None
        await self._device.set_brightness(self._brightness, force=True)
        await self._device.set_label_style(self._cfg.label.model_dump(), force=True)
        await self._render_current_page()
        # Unlock IN_BUTTON streaming for 4th-row HW buttons + encoders.
        # Without this the firmware eats those events.
        await self._device.enable_input_streaming()
        # Logs banner JSON (serial / fw version).
        await self._device.request_device_info()
        self._start_wide_tile_worker()

    async def _event_loop(self) -> None:
        assert self._device is not None
        device = self._device
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            it = device.__aiter__()
            while not self._stop.is_set():
                next_task = asyncio.create_task(it.__anext__())
                done, _pending = await asyncio.wait(
                    {next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    next_task.cancel()
                    return
                try:
                    event = next_task.result()
                except StopAsyncIteration:
                    return
                await self._handle_event(event)
        finally:
            if not stop_task.done():
                stop_task.cancel()

    # ------------------------------------------------------------------ rendering
    async def _render_current_page(self) -> None:
        assert self._device is not None
        page = self._pages.current
        wt_mode = page.wide_tile.mode if page.wide_tile is not None else "clock"
        blob = build_buttons_zip(page.button, self._cfg.label, wide_tile_mode=wt_mode)
        await self._device.push_buttons_zip(blob)
        log.info("pushed page %r (%d buttons)", page.name, len(page.button))

    def _start_wide_tile_worker(self) -> None:
        assert self._device is not None
        page = self._pages.current
        cfg = page.wide_tile or WideTileEntry(mode="clock")
        state = WideTileState(
            config=cfg,
            encoders=list(page.encoder),
            encoder_values={},
        )
        if self._wide is not None:
            self._wide.update_state(state)
            return
        self._wide = WideTileWorker(
            self._device,
            state,
            interval_ms=self._cfg.device.stats_interval_ms,
            provider=self._encoder_value,
        )
        self._wide.start()

    # ------------------------------------------------------------------ event handling
    async def _handle_event(self, event: DeckEvent) -> None:
        if event.kind == DeckEventKind.LCD_BUTTON:
            await self._on_button(event)
        elif event.kind == DeckEventKind.EXTRA_BUTTON:
            await self._on_button(event)
        elif event.kind == DeckEventKind.ENCODER_PRESS:
            await self._on_encoder_press(event)
        elif event.kind == DeckEventKind.ENCODER_ROTATE:
            self._on_encoder_rotate(event)
        elif event.kind == DeckEventKind.DEVICE_INFO:
            log.info("device info: %s", event.info)
        else:
            log.debug("unhandled event: %s extras=%s", event.kind, event.extras)

    async def _on_button(self, event: DeckEvent) -> None:
        pos = event.pos
        if pos is None:
            return
        if pos == WIDE_TILE_POS:
            wt = self._pages.current.wide_tile
            if wt and event.pressed and wt.on_press:
                await dispatch(wt.on_press, ActionContext(self, self._pages.name, "wide_tile"))
            return
        button = next((b for b in self._pages.current.button if b.pos == pos), None)
        if button is None:
            return
        ctx = ActionContext(self, self._pages.name, f"button:{pos}")
        loop = asyncio.get_running_loop()
        if event.pressed:
            state = _PressState(pressed_at=loop.time())
            self._press_state[pos] = state
            if button.on_long_press is not None:
                async def fire_long_press() -> None:
                    try:
                        await asyncio.sleep(button.long_press_ms / 1000.0)
                    except asyncio.CancelledError:
                        return
                    state.long_press_fired = True
                    await dispatch(button.on_long_press, ctx)
                state.long_press_task = asyncio.create_task(fire_long_press())
            else:
                # No long-press configured: fire on_press immediately on the press edge
                if button.on_press is not None:
                    await dispatch(button.on_press, ctx)
        else:
            state = self._press_state.pop(pos, None)
            if state is not None and state.long_press_task is not None:
                state.long_press_task.cancel()
            # If long_press is configured, fire on_press at release iff long-press didn't fire
            if (
                button.on_long_press is not None
                and state is not None
                and not state.long_press_fired
                and button.on_press is not None
            ):
                await dispatch(button.on_press, ctx)
            if button.on_release is not None:
                await dispatch(button.on_release, ctx)

    async def _on_encoder_press(self, event: DeckEvent) -> None:
        idx = event.encoder_index
        if idx is None or not event.pressed:
            return
        enc = next((e for e in self._pages.current.encoder if e.index == idx), None)
        if enc is None or enc.on_press is None:
            return
        await dispatch(enc.on_press, ActionContext(self, self._pages.name, f"encoder:{idx}:press"))

    def _on_encoder_rotate(self, event: DeckEvent) -> None:
        idx = event.encoder_index
        delta = event.delta or 0
        if idx is None or delta == 0:
            return
        self._encoder_accum[idx] = self._encoder_accum.get(idx, 0) + delta
        existing = self._encoder_flush_task.get(idx)
        if existing is not None and not existing.done():
            return
        self._encoder_flush_task[idx] = asyncio.create_task(self._flush_encoder(idx))

    async def _flush_encoder(self, idx: int) -> None:
        await asyncio.sleep(self._cfg.device.encoder_coalesce_ms / 1000.0)
        delta = self._encoder_accum.pop(idx, 0)
        if delta == 0:
            return
        enc = next((e for e in self._pages.current.encoder if e.index == idx), None)
        if enc is None:
            return
        action = enc.on_rotate_cw if delta > 0 else enc.on_rotate_ccw
        if action is None:
            return
        # Repeat the action |delta| times so vol-step etc behaves naturally
        ctx = ActionContext(self, self._pages.name, f"encoder:{idx}:{'cw' if delta > 0 else 'ccw'}")
        for _ in range(abs(delta)):
            await dispatch(action, ctx)

    # ------------------------------------------------------------------ encoder value provider
    async def _encoder_value(self, index: int) -> str:
        enc = next((e for e in self._pages.current.encoder if e.index == index), None)
        if enc is None or enc.value_provider is None:
            return ""
        prov = enc.value_provider
        if isinstance(prov, StaticProvider):
            return prov.value
        if isinstance(prov, BrightnessProvider):
            return f"{self._brightness}%"
        if isinstance(prov, WpctlProvider):
            return await self._wpctl_volume(prov.source)
        if isinstance(prov, ExecProvider):
            return await self._exec_provider(prov)
        return ""

    async def _wpctl_volume(self, source: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "wpctl", "get-volume", source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace").strip()
        # "Volume: 0.42 [MUTED]" -> "42%"
        try:
            after = text.split("Volume:", 1)[1].strip().split()[0]
            pct = int(round(float(after) * 100))
            suffix = " M" if "MUTED" in text else ""
            return f"{pct}%{suffix}"
        except (IndexError, ValueError):
            return text or "?"

    async def _exec_provider(self, prov: ExecProvider) -> str:
        if prov.shell:
            assert isinstance(prov.cmd, str)
            proc = await asyncio.create_subprocess_shell(
                prov.cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
        else:
            argv = shlex.split(prov.cmd) if isinstance(prov.cmd, str) else list(prov.cmd)
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
        out, _ = await proc.communicate()
        return out.decode(errors="replace").strip().splitlines()[0] if out else ""

    # ------------------------------------------------------------------ control surface (used by actions)
    async def switch_page(self, name: str) -> None:
        if self._pages.switch(name) is None:
            return
        await self._render_current_page()
        self._start_wide_tile_worker()

    async def page_back(self) -> None:
        if self._pages.back() is None:
            return
        await self._render_current_page()
        self._start_wide_tile_worker()

    async def page_toggle(self, name: str) -> None:
        if self._pages.toggle(name) is None:
            return
        await self._render_current_page()
        self._start_wide_tile_worker()

    async def cycle_page(self, step: int) -> None:
        names = [p.name for p in self._cfg.page]
        try:
            idx = names.index(self._pages.name)
        except ValueError:
            return
        target = names[(idx + step) % len(names)]
        await self.switch_page(target)

    async def set_brightness(self, value: int) -> None:
        self._brightness = max(0, min(100, int(value)))
        if self._device is not None:
            await self._device.set_brightness(self._brightness)

    async def adjust_brightness(self, delta: int) -> None:
        await self.set_brightness(self._brightness + int(delta))

    async def set_wide_tile_mode(self, mode: str) -> None:
        page = self._pages.current
        prev_mode = page.wide_tile.mode if page.wide_tile is not None else "clock"
        new = (page.wide_tile or WideTileEntry()).model_copy(update={"mode": mode})
        page.wide_tile = new
        if self._wide is not None:
            self._wide.update_state(
                WideTileState(
                    config=new,
                    encoders=list(page.encoder),
                    encoder_values={},
                )
            )
        # Repush the manifest when leaving "background" mode so the leftover
        # wide-tile background image gets overwritten with our solid-black
        # placeholder. Going *into* background mode also repushes, so we drop
        # the placeholder and let the firmware's stored bg show through.
        if prev_mode != mode and (prev_mode == "background" or mode == "background"):
            await self._render_current_page()

    # ------------------------------------------------------------------ config reload
    async def _watch_config(self) -> None:
        try:
            async for _changes in awatch(str(self._config_path), stop_event=self._stop):
                await asyncio.sleep(0.1)  # debounce
                await self._reload()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("config watcher crashed")

    async def _reload(self) -> None:
        try:
            new_cfg = load_config(self._config_path)
        except Exception:  # noqa: BLE001
            log.exception("config reload failed; keeping previous")
            return
        log.info("config reloaded")
        self._cfg = new_cfg
        self._pages.replace_config(new_cfg)
        if self._device is not None:
            await self._device.set_label_style(new_cfg.label.model_dump(), force=True)
            await self._render_current_page()
            self._start_wide_tile_worker()


async def run_service(config_path: Path) -> None:
    svc = Service(config_path)
    loop = asyncio.get_running_loop()
    import signal

    def _signal_handler() -> None:
        log.info("shutdown signal received")
        asyncio.create_task(svc.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass
    await svc.run()
