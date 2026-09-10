"""Run with an isolated Python after installing a built wheel (not an editable)."""

import os
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

import ulanzi_niri
from ulanzi_niri.config import load_config

package = resources.files("ulanzi_niri.data")
for name in ("config.toml", "70-ulanzi-d200x.rules", "ulanzi-niri.service"):
    assert package.joinpath(name).read_text(encoding="utf-8")
assert not package.joinpath("icons").is_dir()
assert Path(ulanzi_niri.__file__).is_relative_to(Path(sys.prefix))

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    env = dict(os.environ, XDG_CONFIG_HOME=str(root / "config"), PYTHONPATH="")
    # Capture systemctl calls without changing the machine's user services.
    binary = root / "bin"
    binary.mkdir()
    systemctl = binary / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$XDG_CONFIG_HOME/systemctl.log"\n')
    systemctl.chmod(0o755)
    env["PATH"] = f"{binary}:{env.get('PATH', '')}"
    command = [sys.executable, "-I", "-m", "ulanzi_niri", "setup", "--no-start"]
    subprocess.run(command, cwd=root, env=env, check=True)
    config = root / "config/ulanzi-niri/config.toml"
    assert load_config(config).page
    config.write_text(config.read_text() + "\n# preserved by repeat setup\n")
    subprocess.run(command, cwd=root, env=env, check=True)
    assert config.read_text().endswith("# preserved by repeat setup\n")
    if Path("/run/systemd/system").is_dir():
        unit = (root / "config/systemd/user/ulanzi-niri.service").read_text()
        assert f'"{sys.executable}" "-m" "ulanzi_niri"' in unit
        assert "@EXEC_START@" not in unit
        assert (root / "config/systemctl.log").read_text().splitlines() == [
            "--user daemon-reload", "--user daemon-reload",
        ]
    else:
        assert not (root / "config/systemd").exists()
        assert not (root / "config/systemctl.log").exists()
print("Installed-package smoke test passed")
