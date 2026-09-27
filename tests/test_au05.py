import asyncio
import errno
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from ulanzi_niri import service
from ulanzi_niri.config import AU05Config, Config, PageConfig
from ulanzi_niri.protocol import ulanzi_au05 as au05
from ulanzi_niri.protocol.manager import HidrawInfo


def node(tmp_path, device, interface):
    hid = tmp_path / device / f"interface{interface}" / f"hid{interface}"
    hid.mkdir(parents=True)
    sysfs = tmp_path / f"hidraw-{device}-{interface}"
    sysfs.mkdir()
    (sysfs / "device").symlink_to(hid)
    return HidrawInfo(Path("/dev") / sysfs.name, sysfs, 0xFFF1, 0xDD, interface)


def fake_nodes(monkeypatch, tmp_path):
    nodes = [node(tmp_path, "au05", interface) for interface in (2, 3)]
    monkeypatch.setattr(au05, "_enumerate_hidraw", Mock(return_value=nodes))
    return nodes


def test_opens_only_both_hid_interfaces_readonly_and_keeps_them_open(monkeypatch, tmp_path):
    nodes = fake_nodes(monkeypatch, tmp_path)
    open_fd = Mock(side_effect=[10, 11])
    close_fd = Mock()
    monkeypatch.setattr(au05.os, "open", open_fd)
    monkeypatch.setattr(au05.os, "close", close_fd)
    device = au05.open_au05()
    assert device is not None
    au05._enumerate_hidraw.assert_called_once_with(0xFFF1, 0xDD)
    assert [call.args[0] for call in open_fd.call_args_list] == [
        nodes[0].dev_path, nodes[1].dev_path
    ]
    assert all(call.args[1] == os.O_RDONLY | os.O_NONBLOCK for call in open_fd.call_args_list)
    close_fd.assert_not_called()
    device.close()
    device.close()
    assert [call.args[0] for call in close_fd.call_args_list] == [11, 10]


@pytest.mark.parametrize("failure", ["input", "vendor"])
def test_open_failure_closes_partial_connection(monkeypatch, tmp_path, failure):
    fake_nodes(monkeypatch, tmp_path)
    error = PermissionError("denied")
    monkeypatch.setattr(au05.os, "open", Mock(side_effect=[error] if failure == "input" else [10, error]))
    close_fd = Mock()
    monkeypatch.setattr(au05.os, "close", close_fd)
    assert au05.open_au05() is None
    assert [call.args[0] for call in close_fd.call_args_list] == ([] if failure == "input" else [10])


@pytest.mark.parametrize("interfaces", [[], [("first", 2)], [("first", 3)], [("first", 2), ("second", 3)]])
def test_requires_interface_pair_from_same_device(monkeypatch, tmp_path, interfaces):
    nodes = [node(tmp_path, device, interface) for device, interface in interfaces]
    monkeypatch.setattr(au05, "_enumerate_hidraw", lambda *_: nodes)
    open_fd = Mock()
    monkeypatch.setattr(au05.os, "open", open_fd)
    assert au05.open_au05() is None
    open_fd.assert_not_called()


@pytest.mark.parametrize("disconnected_fd", [10, 11])
@pytest.mark.parametrize("disconnect", [b"", OSError(errno.ENODEV, "gone")])
async def test_drains_raw_copies_and_detects_disconnect_without_writes(monkeypatch, disconnected_fd, disconnect):
    packets = {10: [bytes.fromhex("03 40 00 2c 00 00 00 00 00")], 11: [b"\x55" * 64]}
    packets[disconnected_fd].append(disconnect)

    def read(fd, size):
        if not packets[fd]:
            raise BlockingIOError
        result = packets[fd].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(au05.os, "read", read)
    write = Mock()
    monkeypatch.setattr(au05.os, "write", write)
    device = au05.UlanziAU05Device(10, 11)
    with pytest.raises(OSError):
        await device.keep_alive()
    write.assert_not_called()


@pytest.mark.parametrize("busy", [False, True])
async def test_idle_and_busy_endpoints_yield_without_closing_handles(monkeypatch, busy):
    def read(fd, size):
        if busy:
            return b"\x55" * 64
        raise BlockingIOError

    read_fd = Mock(side_effect=read)
    close_fd = Mock()
    monkeypatch.setattr(au05.os, "read", read_fd)
    monkeypatch.setattr(au05.os, "close", close_fd)
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(au05.asyncio, "sleep", sleep)
    device = au05.UlanziAU05Device(10, 11)
    with pytest.raises(asyncio.CancelledError):
        await device.keep_alive()
    assert read_fd.call_count == (64 if busy else 2)
    sleep.assert_awaited_once()
    close_fd.assert_not_called()
    device.close()
    assert close_fd.call_count == 2


def test_config_is_keepalive_only_and_opt_in():
    assert not Config(page=[PageConfig(name="main")]).au05.enabled
    assert AU05Config(enabled=True).enabled
    with pytest.raises(ValidationError):
        AU05Config(enabled=True, grab_input=True)


def make_service(monkeypatch, *, enabled=False):
    cfg = Config(page=[PageConfig(name="main")], au05=AU05Config(enabled=enabled))
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    return service.Service(Path("unused.toml"))


async def test_reload_enable_disable_and_unrelated_changes_preserve_keepalive(monkeypatch):
    app = make_service(monkeypatch)
    connected, closed = asyncio.Event(), asyncio.Event()
    open_device = Mock()
    monkeypatch.setattr(service, "open_au05", open_device)
    await app._sync_au05()
    assert app._au05_task is None
    open_device.assert_not_called()

    async def run():
        connected.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()

    monkeypatch.setattr(app, "_run_au05", run)
    app._cfg.au05.enabled = True
    await app._reload()
    await asyncio.wait_for(connected.wait(), 1)
    original = app._au05_task
    await app._reload()
    assert app._au05_task is original
    assert not closed.is_set()
    app._cfg.au05.enabled = False
    await app._reload()
    assert closed.is_set()
    assert app._au05_task is None


async def test_stop_closes_keepalive_handles(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    started = asyncio.Event()

    async def keep_alive():
        started.set()
        await asyncio.Future()

    device = Mock(keep_alive=keep_alive)
    monkeypatch.setattr(service, "open_au05", Mock(return_value=device))
    await app._sync_au05()
    await asyncio.wait_for(started.wait(), 1)
    await app.stop()
    device.close.assert_called_once()
    assert app._au05_task is None


async def test_run_starts_au05_while_waiting_for_d200x(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    waiting_for_deck = asyncio.Event()
    au05_started = asyncio.Event()

    async def wait_for_deck():
        waiting_for_deck.set()
        await app._stop.wait()

    async def run_au05():
        au05_started.set()
        await app._stop.wait()

    async def watch():
        await app._stop.wait()

    monkeypatch.setattr(app, "start_control", AsyncMock())
    monkeypatch.setattr(app, "stop_control", AsyncMock())
    monkeypatch.setattr(app, "_watch_config", watch)
    monkeypatch.setattr(app, "_connect_and_serve", wait_for_deck)
    monkeypatch.setattr(app, "_run_au05", run_au05)
    task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(waiting_for_deck.wait(), 1)
        await asyncio.wait_for(au05_started.wait(), 1)
    finally:
        await app.stop()
        await asyncio.wait_for(task, 1)


async def test_failed_reload_keeps_running_au05(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    previous = app._cfg
    monkeypatch.setattr(service, "load_config", Mock(side_effect=ValueError("bad config")))
    sync = AsyncMock()
    monkeypatch.setattr(app, "_sync_au05", sync)
    await app._reload()
    assert app._cfg is previous
    sync.assert_not_awaited()


async def test_disconnect_retries_and_closes_old_handles(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    first = Mock(keep_alive=AsyncMock(side_effect=OSError(errno.ENODEV, "unplugged")))
    second = Mock(keep_alive=AsyncMock(side_effect=app._stop.set))
    opened = Mock(side_effect=[None, first, second])
    monkeypatch.setattr(service, "open_au05", opened)
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    await app._run_au05()
    assert opened.call_count == 3
    first.close.assert_called_once()
    second.close.assert_called_once()
