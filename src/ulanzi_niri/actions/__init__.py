"""Action dispatch.

Executes any `config.Action` against a Service context. All actions are
async and `dispatch()` swallows exceptions \u2014 a misbehaving action must not
crash the worker loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import (
    Action,
    BrightnessAction,
    ExecAction,
    KeysAction,
    MediaAction,
    NiriAction,
    NoopAction,
    PageAction,
    SmallWindowAction,
    UrlAction,
)

if TYPE_CHECKING:
    from ..service import Service

log = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()


@dataclass
class ActionContext:
    service: Service
    page_name: str
    source: str  # "button:5", "encoder:0:press", "encoder:0:cw", ...


async def _run_argv(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 15.0) -> int:
    log.debug("exec %s", argv)
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
    except FileNotFoundError:
        log.error("command not found: %s", argv[0])
        return 127
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        log.error("command timed out: %s", argv)
        return 124
    rc = proc.returncode or 0
    if rc != 0:
        log.warning("exit %d: %s\n%s", rc, argv, stderr.decode(errors="replace").rstrip())
    return rc


async def _run_shell(cmd: str, *, env: dict[str, str] | None = None, timeout: float = 15.0) -> int:
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        log.error("shell timed out: %s", cmd)
        return 124
    rc = proc.returncode or 0
    if rc != 0:
        log.warning("shell exit %d: %s\n%s", rc, cmd, stderr.decode(errors="replace").rstrip())
    return rc


async def dispatch(action: Action | None, ctx: ActionContext) -> None:
    if action is None:
        return
    try:
        if isinstance(action, NoopAction):
            return
        if isinstance(action, NiriAction):
            await _do_niri(action)
        elif isinstance(action, ExecAction):
            task = asyncio.create_task(
                _do_exec_background(action, ctx),
                name=f"exec:{ctx.source}",
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        elif isinstance(action, UrlAction):
            await _do_url(action)
        elif isinstance(action, MediaAction):
            await _do_media(action)
        elif isinstance(action, KeysAction):
            await _do_keys(action)
        elif isinstance(action, PageAction):
            await _do_page(action, ctx)
        elif isinstance(action, BrightnessAction):
            await _do_brightness(action, ctx)
        elif isinstance(action, SmallWindowAction):
            await ctx.service.set_wide_tile_mode(action.mode)
        else:
            log.warning("unhandled action: %r", action)
    except Exception:  # noqa: BLE001
        log.exception("action failed (source=%s, action=%r)", ctx.source, action)


async def _do_niri(action: NiriAction) -> None:
    argv = ["niri", "msg", "action"]
    if action.unsafe_raw:
        argv.extend(shlex.split(action.action))
    else:
        argv.append(action.action)
        argv.extend(action.args)
    await _run_argv(argv)


async def _do_exec(action: ExecAction) -> None:
    if action.shell:
        if not isinstance(action.cmd, str):
            log.warning("exec.shell=true requires cmd as string")
            return
        await _run_shell(action.cmd, env=action.env)
        return
    argv = shlex.split(action.cmd) if isinstance(action.cmd, str) else list(action.cmd)
    if not argv:
        return
    await _run_argv(argv, env=action.env)


async def _do_exec_background(action: ExecAction, ctx: ActionContext) -> None:
    try:
        await _do_exec(action)
    except Exception:  # noqa: BLE001
        log.exception("action failed (source=%s, action=%r)", ctx.source, action)


async def _do_url(action: UrlAction) -> None:
    opener = action.opener or "xdg-open"
    if not shutil.which(opener):
        log.error("url opener not found: %s", opener)
        return
    await _run_argv([opener, action.url])


async def _do_media(action: MediaAction) -> None:
    sub = action.cmd
    if sub == "mute":
        await _run_argv(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
        return
    if sub == "vol-up":
        await _run_argv(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{action.step}%+"])
        return
    if sub == "vol-down":
        await _run_argv(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{action.step}%-"])
        return
    argv = ["playerctl"]
    if action.player:
        argv.extend(["-p", action.player])
    # playerctl uses "previous" not "prev"
    argv.append("previous" if sub == "prev" else sub)
    await _run_argv(argv)


async def _do_keys(action: KeysAction) -> None:
    backend = action.backend
    if backend == "auto":
        backend = "wtype" if shutil.which("wtype") else ("ydotool" if shutil.which("ydotool") else "auto")
    if backend == "wtype":
        await _run_argv(["wtype", action.keys])
    elif backend == "ydotool":
        await _run_argv(["ydotool", "type", action.keys])
    else:
        log.error("no keys backend available (install wtype or ydotool)")


async def _do_page(action: PageAction, ctx: ActionContext) -> None:
    if action.back:
        await ctx.service.page_back()
    elif action.toggle:
        await ctx.service.page_toggle(action.toggle)
    elif action.goto:
        await ctx.service.switch_page(action.goto)
    elif action.cycle:
        await ctx.service.cycle_page(action.cycle)


async def _do_brightness(action: BrightnessAction, ctx: ActionContext) -> None:
    if action.set is not None:
        await ctx.service.set_brightness(action.set)
    elif action.delta is not None:
        await ctx.service.adjust_brightness(action.delta)
