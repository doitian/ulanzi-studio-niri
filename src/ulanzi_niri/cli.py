"""ulanzi-niri command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from importlib import resources
from pathlib import Path

from .config import default_config_path, load_config
from .log import configure as configure_logging
from .protocol.manager import find_device_path, open_device

log = logging.getLogger(__name__)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config", "-c", type=Path, default=None, help="path to config.toml (defaults to XDG)"
    )
    p.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ulanzi-niri", description="Ulanzi D200X driver for niri")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the daemon (default for systemd unit)")
    _add_common(p_run)

    p_doctor = sub.add_parser("doctor", help="report environment + device status")
    _add_common(p_doctor)

    p_render = sub.add_parser("render", help="render the current page to PNG files (no device)")
    _add_common(p_render)
    p_render.add_argument("--out", type=Path, default=Path("/tmp/ulanzi-render"))
    p_render.add_argument("--page", default=None, help="page name (default: default page)")

    p_push = sub.add_parser("push", help="render a page and push it to the device once")
    _add_common(p_push)
    p_push.add_argument("--page", default=None)

    p_brightness = sub.add_parser("brightness", help="set device brightness (0..100)")
    _add_common(p_brightness)
    p_brightness.add_argument("value", type=int)

    p_sniff = sub.add_parser("sniff", help="dump raw input events from the device")
    _add_common(p_sniff)
    p_sniff.add_argument("--seconds", type=float, default=0.0, help="0 = run forever")

    sub.add_parser("install-udev", help="print the sudo commands to install the udev rule")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(getattr(args, "log_level", None))

    cmd = args.cmd
    if cmd == "run":
        return _cmd_run(args)
    if cmd == "doctor":
        return _cmd_doctor(args)
    if cmd == "render":
        return _cmd_render(args)
    if cmd == "push":
        return _cmd_push(args)
    if cmd == "brightness":
        return _cmd_brightness(args)
    if cmd == "sniff":
        return _cmd_sniff(args)
    if cmd == "install-udev":
        return _cmd_install_udev()
    return 2


# ---------------------------------------------------------------------------- handlers
def _cmd_run(args: argparse.Namespace) -> int:
    from .service import run_service

    cfg_path = args.config or default_config_path()
    if not cfg_path.exists():
        print(f"error: config not found at {cfg_path}", file=sys.stderr)
        print("hint: cp examples/config.toml ~/.config/ulanzi-niri/config.toml", file=sys.stderr)
        return 1
    asyncio.run(run_service(cfg_path))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    print(f"python:        {sys.executable}")
    print(f"version:       {sys.version.split()[0]}")
    cfg_path = args.config or default_config_path()
    print(f"config path:   {cfg_path} ({'present' if cfg_path.exists() else 'MISSING'})")
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            print(f"config:        ok ({len(cfg.page)} page(s))")
            print(f"default page:  {cfg.default_page().name}")
        except Exception as exc:  # noqa: BLE001
            print(f"config:        ERROR {exc}")
    path = find_device_path()
    if path is None:
        print("device:        not detected (vid:pid 2207:0019 not enumerated)")
    else:
        print(f"device path:   {path!r}")
        dev = open_device()
        if dev is None:
            print("device open:   FAILED (likely missing udev rule; run install-udev)")
        else:
            print("device open:   ok")
            dev.close()
    for tool in ("niri", "playerctl", "wpctl", "grim", "grimblast", "wtype", "ydotool", "slurp"):
        from shutil import which
        present = which(tool)
        print(f"  {tool:10}{'present at ' + present if present else 'not found'}")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    from .icons import render_cached, request_from_button
    from .protocol.ulanzi_d200x import BUTTON_GEOMETRY

    cfg = load_config(args.config or default_config_path())
    page = cfg.get_page(args.page) if args.page else cfg.default_page()
    if page is None:
        print(f"no such page: {args.page}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    by_pos = {b.pos: b for b in page.button}
    for pos, geom in BUTTON_GEOMETRY.items():
        b = by_pos.get(pos)
        if b is None:
            continue
        req = request_from_button(b.label, b.icon, geom.width, geom.height, cfg.label)
        cached = render_cached(req)
        target = args.out / f"pos-{pos:02d}.png"
        target.write_bytes(cached.read_bytes())
        print(f"wrote {target}")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    from .zip_builder import build_buttons_zip

    cfg = load_config(args.config or default_config_path())
    page = cfg.get_page(args.page) if args.page else cfg.default_page()
    if page is None:
        print(f"no such page: {args.page}", file=sys.stderr)
        return 1
    dev = open_device()
    if dev is None:
        print("device not available (check connection and udev rule)", file=sys.stderr)
        return 1
    try:
        async def _push() -> None:
            await dev.set_brightness(cfg.device.brightness, force=True)
            await dev.set_label_style(cfg.label.model_dump(), force=True)
            blob = build_buttons_zip(page.button, cfg.label)
            await dev.push_buttons_zip(blob)
        asyncio.run(_push())
    finally:
        dev.close()
    print(f"pushed page {page.name!r}")
    return 0


def _cmd_brightness(args: argparse.Namespace) -> int:
    if not (0 <= args.value <= 100):
        print("brightness must be 0..100", file=sys.stderr)
        return 2
    dev = open_device()
    if dev is None:
        print("device not available", file=sys.stderr)
        return 1
    try:
        asyncio.run(dev.set_brightness(args.value, force=True))
    finally:
        dev.close()
    return 0


def _cmd_sniff(args: argparse.Namespace) -> int:
    dev = open_device()
    if dev is None:
        print("device not available", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None

    async def _loop() -> None:
        async for ev in dev:
            print(
                f"{ev.kind.value:14} pos={ev.pos} encoder={ev.encoder_index} "
                f"pressed={ev.pressed} delta={ev.delta} extras={ev.extras} "
                f"raw={(ev.raw or b'')[:16].hex()}"
            )
            if deadline is not None and time.monotonic() >= deadline:
                break

    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
    return 0


def _cmd_install_udev() -> int:
    rule = resources.files("ulanzi_niri.data").joinpath("70-ulanzi-d200x.rules")
    rule_text = rule.read_text()
    target = "/etc/udev/rules.d/70-ulanzi-d200x.rules"
    print("# To install the udev rule, run the following as root:")
    print()
    print(f"sudo tee {target} > /dev/null <<'EOF'")
    print(rule_text.rstrip())
    print("EOF")
    print("sudo udevadm control --reload-rules")
    print("sudo udevadm trigger --subsystem-match=hidraw")
    print()
    print("# Make sure your user is in the 'plugdev' group:")
    print(f"#   sudo usermod -aG plugdev {os.environ.get('USER', '$USER')}")
    print("#   (then log out + back in)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
