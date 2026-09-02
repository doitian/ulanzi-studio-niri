from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ulanzi_niri import actions
from ulanzi_niri.actions import ActionContext
from ulanzi_niri.config import ExecAction


async def test_exec_dispatch_does_not_wait_for_process_exit(monkeypatch) -> None:
    process_started = asyncio.Event()
    process_release = asyncio.Event()
    process_finished = asyncio.Event()

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            process_started.set()
            await process_release.wait()
            process_finished.set()
            return b"", b""

    async def create_subprocess_exec(*args, **kwargs) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(actions.asyncio, "create_subprocess_exec", create_subprocess_exec)
    action = ExecAction(type="exec", cmd=["long-running-command"])
    ctx = ActionContext(service=SimpleNamespace(), page_name="main", source="button:0")

    dispatch_task = asyncio.create_task(actions.dispatch(action, ctx))
    try:
        await asyncio.wait_for(process_started.wait(), timeout=1)
        assert dispatch_task.done()
        await dispatch_task
        assert not process_finished.is_set()
    finally:
        process_release.set()
        await asyncio.wait_for(process_finished.wait(), timeout=1)
