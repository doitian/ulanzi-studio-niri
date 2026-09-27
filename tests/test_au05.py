import asyncio
import errno
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from ulanzi_niri import service
from ulanzi_niri.config import AU05Config, AU05KeyEntry, Config, PageConfig
from ulanzi_niri.protocol import ulanzi_au05 as au05
from ulanzi_niri.protocol.manager import HidrawInfo


def keyboard(*codes, modifiers=0):
    return bytes([3, modifiers, 0, *codes, *([0] * (6 - len(codes)))])


def test_keyboard_edges_and_simultaneous_keys():
    decoder = au05.AU05Decoder()
    assert decoder.decode(keyboard(1, 0x28)) == [
        au05.AU05Event("keyboard", 1, True),
        au05.AU05Event("keyboard", 0x28, True),
    ]
    assert decoder.decode(keyboard(1, 0x28)) == []
    assert decoder.decode(keyboard(0x29)) == [
        au05.AU05Event("keyboard", 1, False),
        au05.AU05Event("keyboard", 0x28, False),
        au05.AU05Event("keyboard", 0x29, True),
    ]
    assert decoder.decode(keyboard()) == [au05.AU05Event("keyboard", 0x29, False)]


@pytest.mark.parametrize("delta", [-128, -3, -1, 0, 1, 4, 127])
def test_wheel_signed_delta(delta):
    packet = bytes([2, 0, 0, 0, delta & 255, 0])
    expected = [au05.AU05Event("wheel", delta=delta)] if delta else []
    assert au05.AU05Decoder().decode(packet) == expected


def test_mouse_consumer_and_keyboard_states_are_independent():
    decoder = au05.AU05Decoder()
    assert decoder.decode(keyboard(1)) == [au05.AU05Event("keyboard", 1, True)]
    assert decoder.decode(bytes.fromhex("02 04 00 00 00 00")) == [
        au05.AU05Event("mouse", 3, True)
    ]
    assert decoder.decode(bytes.fromhex("01 e2 00")) == [
        au05.AU05Event("consumer", 0xE2, True)
    ]
    assert decoder.decode(bytes.fromhex("01 00 00")) == [
        au05.AU05Event("consumer", 0xE2, False)
    ]
    assert decoder.decode(keyboard()) == [au05.AU05Event("keyboard", 1, False)]
    assert decoder.decode(bytes.fromhex("02 00 00 00 00 00")) == [
        au05.AU05Event("mouse", 3, False)
    ]


@pytest.mark.parametrize("packet", [b"", b"\x03", b"\x03\x00\x00\x00", b"\x02\x00", b"\x01", b"\x55" * 64])
def test_truncated_and_unknown_reports_do_not_release_held_keys(packet):
    decoder = au05.AU05Decoder()
    decoder.decode(keyboard(1))
    assert decoder.decode(packet) == []
    assert decoder.decode(keyboard()) == [au05.AU05Event("keyboard", 1, False)]


def node(tmp_path, device, interface):
    hid = tmp_path / device / f"interface{interface}" / f"hid{interface}"
    hid.mkdir(parents=True)
    sysfs = tmp_path / f"hidraw-{device}-{interface}"
    sysfs.mkdir()
    (sysfs / "device").symlink_to(hid)
    return HidrawInfo(Path("/dev") / sysfs.name, sysfs, 0xFFF1, 0xDD, interface)


def fake_nodes(monkeypatch, tmp_path):
    nodes = [node(tmp_path, "au05", interface) for interface in (2, 3)]
    hid = (nodes[0].sysfs / "device").resolve()
    event = hid / "input/input20/event20"
    event.mkdir(parents=True)
    monkeypatch.setattr(au05, "_enumerate_hidraw", Mock(return_value=nodes))
    return nodes


def test_opens_both_keepalive_interfaces_and_grabs_only_au05(monkeypatch, tmp_path):
    nodes = fake_nodes(monkeypatch, tmp_path)
    open_fd = Mock(side_effect=[10, 11, 12])
    close_fd, ioctl = Mock(), Mock()
    monkeypatch.setattr(au05.os, "open", open_fd)
    monkeypatch.setattr(au05.os, "close", close_fd)
    monkeypatch.setattr(au05.fcntl, "ioctl", ioctl)
    device = au05.open_au05()
    assert device is not None
    au05._enumerate_hidraw.assert_called_once_with(0xFFF1, 0xDD)
    assert [call.args[0] for call in open_fd.call_args_list] == [
        nodes[0].dev_path, nodes[1].dev_path, Path("/dev/input/event20")
    ]
    assert all(call.args[1] == os.O_RDONLY | os.O_NONBLOCK for call in open_fd.call_args_list)
    ioctl.assert_called_once_with(12, au05.EVIOCGRAB, 1)
    close_fd.assert_not_called()
    device.close()
    device.close()
    assert [call.args[0] for call in close_fd.call_args_list] == [12, 11, 10]


@pytest.mark.parametrize("failure", ["vendor", "grab"])
def test_partial_open_failure_closes_every_descriptor(monkeypatch, tmp_path, failure):
    fake_nodes(monkeypatch, tmp_path)
    error = PermissionError("denied")
    monkeypatch.setattr(au05.os, "open", Mock(side_effect=[10, error] if failure == "vendor" else [10, 11, 12]))
    monkeypatch.setattr(au05.fcntl, "ioctl", Mock(side_effect=error))
    close_fd = Mock()
    monkeypatch.setattr(au05.os, "close", close_fd)
    assert au05.open_au05() is None
    assert [call.args[0] for call in close_fd.call_args_list] == ([10] if failure == "vendor" else [12, 11, 10])


def test_never_pairs_interfaces_from_different_devices(monkeypatch, tmp_path):
    nodes = [node(tmp_path, "first", 2), node(tmp_path, "second", 3)]
    monkeypatch.setattr(au05, "_enumerate_hidraw", lambda *_: nodes)
    open_fd = Mock()
    monkeypatch.setattr(au05.os, "open", open_fd)
    assert au05.open_au05() is None
    open_fd.assert_not_called()


def test_grab_can_be_disabled_without_disabling_keepalive(monkeypatch, tmp_path):
    fake_nodes(monkeypatch, tmp_path)
    open_fd = Mock(side_effect=[10, 11])
    monkeypatch.setattr(au05.os, "open", open_fd)
    monkeypatch.setattr(au05.os, "close", Mock())
    ioctl = Mock()
    monkeypatch.setattr(au05.fcntl, "ioctl", ioctl)
    device = au05.open_au05(grab_input=False)
    assert device is not None
    assert open_fd.call_count == 2
    ioctl.assert_not_called()
    device.close()


async def test_reads_vendor_heartbeat_and_detects_disconnect(monkeypatch):
    packets = {10: [keyboard(1)], 11: [b"\x55" * 64, OSError(errno.ENODEV, "gone")]}
    reads = []

    def read(fd, size):
        reads.append(fd)
        if not packets[fd]:
            raise BlockingIOError
        result = packets[fd].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(au05.os, "read", read)
    device = au05.UlanziAU05Device(10, 11, [])
    iterator = device.__aiter__()
    assert await anext(iterator) == au05.AU05Event("keyboard", 1, True)
    with pytest.raises(OSError):
        await anext(iterator)
    assert 11 in reads


def test_config_opt_in():
    assert not Config(page=[PageConfig(name="main")]).au05.enabled
    cfg = AU05Config.model_validate({
        "enabled": True,
        "key": [{"index": i, "code": code, "on_press": {"type": "noop"}}
                for i, code in enumerate([1, 0x28, 0x29, 0xE2], 1)],
        "wheel": {"on_rotate_cw": {"type": "media", "cmd": "vol-up"},
                  "on_rotate_ccw": {"type": "media", "cmd": "vol-down"}},
    })
    assert len(cfg.key) == 4
    assert cfg.wheel.on_rotate_ccw.cmd == "vol-down"


@pytest.mark.parametrize("entry", [
    {"index": 0, "code": 1}, {"index": 5, "code": 1},
    {"index": 1, "code": 0}, {"index": 1, "code": 256},
    {"index": 1, "report": "mouse", "code": 9},
    {"index": 1, "report": "consumer", "code": 65536},
    {"index": 1, "code": 1, "on_pres": {"type": "noop"}},
])
def test_invalid_bindings(entry):
    with pytest.raises(ValidationError):
        AU05KeyEntry.model_validate(entry)


@pytest.mark.parametrize("entries", [
    [{"index": 1, "code": 1}, {"index": 1, "code": 2}],
    [{"index": 1, "code": 1}, {"index": 2, "code": 1}],
])
def test_duplicate_bindings(entries):
    with pytest.raises(ValidationError):
        AU05Config(key=entries)


def make_service(monkeypatch, **au05_config):
    cfg = Config(page=[PageConfig(name="main")], au05=AU05Config(**au05_config))
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    return service.Service(Path("unused.toml"))


async def test_all_controls_dispatch_without_d200x(monkeypatch):
    app = make_service(monkeypatch, enabled=True, key=[
        {"index": i, "code": code, "on_press": {"type": "noop"},
         "on_release": {"type": "noop"}}
        for i, code in enumerate([1, 0x28, 0x29, 0xE2], 1)
    ], wheel={"on_rotate_cw": {"type": "media", "cmd": "vol-up"},
              "on_rotate_ccw": {"type": "media", "cmd": "vol-down"}})
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch", dispatch)
    for key in app._cfg.au05.key:
        for pressed in (True, False):
            await app._handle_au05_event(au05.AU05Event("keyboard", key.code, pressed))
    await app._handle_au05_event(au05.AU05Event("wheel", delta=2))
    await app._handle_au05_event(au05.AU05Event("wheel", delta=-3))
    assert dispatch.await_count == 13
    assert dispatch.call_args.args[0].cmd == "vol-down"
    assert dispatch.call_args.args[1].source == "au05:wheel:ccw"
    assert app._device is None


async def test_reload_enable_disable_and_binding_changes_preserve_keepalive(monkeypatch):
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


async def test_cancel_closes_device_while_action_is_pending(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    action_started = asyncio.Event()

    async def events():
        yield au05.AU05Event("keyboard", 1, True)
        await asyncio.Future()

    device = Mock()
    device.__aiter__ = lambda _: events()
    monkeypatch.setattr(service, "open_au05", Mock(return_value=device))

    async def handle(event):
        action_started.set()
        await asyncio.Future()

    monkeypatch.setattr(app, "_handle_au05_event", handle)
    await app._sync_au05()
    await asyncio.wait_for(action_started.wait(), 1)
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


async def test_grab_change_restarts_au05(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    new_cfg = app._cfg.model_copy(deep=True)
    new_cfg.au05.grab_input = False
    monkeypatch.setattr(service, "load_config", lambda _: new_cfg)
    sync = AsyncMock()
    monkeypatch.setattr(app, "_sync_au05", sync)
    await app._reload()
    sync.assert_awaited_once_with(restart=True)


async def test_disabled_or_unbound_events_do_not_dispatch(monkeypatch):
    app = make_service(monkeypatch)
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch", dispatch)
    await app._handle_au05_event(au05.AU05Event("wheel", delta=1))
    app._cfg.au05.enabled = True
    await app._handle_au05_event(au05.AU05Event("keyboard", 123, True))
    dispatch.assert_not_awaited()


async def test_disconnect_retries_and_closes_old_handles(monkeypatch):
    app = make_service(monkeypatch, enabled=True)
    first, second = Mock(), Mock()
    opened = Mock(side_effect=[None, first, second])
    monkeypatch.setattr(service, "open_au05", opened)

    async def serve(device):
        if device is first:
            raise OSError(errno.ENODEV, "unplugged")
        app._stop.set()

    monkeypatch.setattr(app, "_serve_au05", serve)
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    await app._run_au05()
    assert opened.call_count == 3
    first.close.assert_called_once()
    second.close.assert_called_once()
