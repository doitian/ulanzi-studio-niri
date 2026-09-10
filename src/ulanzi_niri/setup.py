"""Install the resources shipped with the package for the current user."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from .config import default_config_path, load_config

RULE_PATH = Path("/etc/udev/rules.d/70-ulanzi-d200x.rules")
SERVICE_NAME = "ulanzi-niri.service"
SYSTEMD_RUNTIME = Path("/run/systemd/system")


def _resource(name: str) -> str:
    return resources.files("ulanzi_niri.data").joinpath(name).read_text(encoding="utf-8")


def install_config() -> None:
    target = default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(_resource("config.toml"))
    except FileExistsError:
        print(f"Keeping existing config: {target}")
    else:
        print(f"Installed config: {target}")


def udev_configured() -> bool:
    """Check whether the installed rule matches the version shipped with us."""
    try:
        return RULE_PATH.read_text(encoding="utf-8") == _resource(RULE_PATH.name)
    except (OSError, UnicodeError):
        return False


def install_udev() -> None:
    if not udev_configured():
        RULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RULE_PATH.write_text(_resource(RULE_PATH.name), encoding="utf-8")
        RULE_PATH.chmod(0o644)
    # Reload even on a repeat run: a prior attempt may have failed after writing.
    subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
    subprocess.run(["udevadm", "trigger", "--subsystem-match=hidraw"], check=True)
    print("Installed udev rule. Replug the deck if access is not available yet.")


def admin_setup() -> int:
    if os.geteuid() != 0:
        print("error: run sudo ulanzi-niri admin-setup to install the udev rule", file=sys.stderr)
        return 1
    try:
        install_udev()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: admin-setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _unit_quote(value: str) -> str:
    """Quote a systemd argument, including specifier/environment expansion."""
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("%", "%%").replace("$", "$$")
    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{value}"'


def install_service(*, start: bool) -> None:
    if shutil.which("systemctl") is None or not SYSTEMD_RUNTIME.is_dir():
        print(
            "warning: systemd is not available; skipping user service setup. "
            "Run ulanzi-niri run to start the daemon manually.",
            file=sys.stderr,
        )
        return
    config = default_config_path().absolute()
    if start:
        # Avoid enabling a daemon that cannot start, especially with --no-config.
        load_config(config)
    target = config.parent.parent / "systemd" / "user" / SERVICE_NAME
    # Do not resolve symlinks: a venv's Python symlink identifies its environment.
    command = " ".join(
        _unit_quote(arg)
        for arg in (os.path.abspath(sys.executable), "-m", "ulanzi_niri", "run", "--config", str(config))
    )
    unit = _resource(SERVICE_NAME).replace("@EXEC_START@", command)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit, encoding="utf-8")
    print(f"Installed service: {target}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if start:
        subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME], check=True)
        subprocess.run(["systemctl", "--user", "restart", SERVICE_NAME], check=True)
        print("Enabled and started service.")


def setup(args: argparse.Namespace) -> int:
    if os.geteuid() == 0:
        print("error: run setup as your desktop user; use admin-setup for udev",
              file=sys.stderr)
        return 1
    if not udev_configured():
        print("udev rule missing, outdated, or unreadable; run sudo ulanzi-niri admin-setup",
              file=sys.stderr)
    try:
        if not args.no_config:
            install_config()
        if not args.no_service:
            install_service(start=not args.no_start)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: setup failed: {exc}", file=sys.stderr)
        return 1
    return 0
