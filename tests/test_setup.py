"""Setup behavior without changing the host's udev or systemd configuration."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from ulanzi_niri import cli, setup
from ulanzi_niri.config import load_config


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup, "SYSTEMD_RUNTIME", tmp_path)
    run = Mock()
    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(setup, "RULE_PATH", tmp_path / "udev" / "70-ulanzi-d200x.rules")
    return tmp_path, run


def test_setup_defaults(environment):
    root, run = environment
    assert cli.main(["setup"]) == 0
    assert load_config(root / "ulanzi-niri/config.toml").default_page().name == "main"
    unit = (root / "systemd/user/ulanzi-niri.service").read_text()
    assert '@EXEC_START@' not in unit
    assert f'"{setup.sys.executable}" "-m" "ulanzi_niri" "run"' in unit
    assert f'"{root}/ulanzi-niri/config.toml"' in unit
    commands = [call.args[0] for call in run.call_args_list]
    assert all(command[0] == "systemctl" for command in commands)
    assert not setup.RULE_PATH.exists()
    assert commands[-2:] == [
        ["systemctl", "--user", "enable", "ulanzi-niri.service"],
        ["systemctl", "--user", "restart", "ulanzi-niri.service"],
    ]


@pytest.mark.parametrize("component", ["config", "service"])
def test_skip_component(environment, monkeypatch, component):
    handlers = {name: Mock() for name in ("config", "udev", "service")}
    for name, handler in handlers.items():
        monkeypatch.setattr(setup, f"install_{name}", handler)
    assert cli.main(["setup", f"--no-{component}"]) == 0
    for name, handler in handlers.items():
        assert handler.call_count == (0 if name in (component, "udev") else 1)


def test_skip_all(environment):
    root, run = environment
    assert cli.main(["setup", "--no-config", "--no-service"]) == 0
    assert not list(root.iterdir())
    run.assert_not_called()


def test_no_start(environment):
    _, run = environment
    assert cli.main(["setup", "--no-start"]) == 0
    run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], check=True)


@pytest.mark.parametrize("symlink", [False, True])
def test_preserve_config(environment, symlink):
    root, _ = environment
    target = root / "ulanzi-niri/config.toml"
    target.parent.mkdir()
    if symlink:
        target.symlink_to(root / "missing.toml")
    else:
        target.write_text("# custom config\n")
    assert cli.main(["setup", "--no-service"]) == 0
    if symlink:
        assert target.is_symlink()
        assert not target.exists()
    else:
        assert target.read_text() == "# custom config\n"


def test_missing_config_does_not_enable_service(environment, capsys):
    _, run = environment
    assert cli.main(["setup", "--no-config"]) == 1
    run.assert_not_called()
    assert "setup failed" in capsys.readouterr().err


def test_subprocess_failure(environment, capsys):
    _, run = environment
    run.side_effect = setup.subprocess.CalledProcessError(1, ["systemctl"])
    assert cli.main(["setup"]) == 1
    assert "setup failed" in capsys.readouterr().err


def test_root_user_setup_rejected(environment, monkeypatch, capsys):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    assert cli.main(["setup"]) == 1
    assert "desktop user" in capsys.readouterr().err


def test_existing_rule_still_reloads(environment):
    _, run = environment
    setup.RULE_PATH.parent.mkdir()
    setup.RULE_PATH.write_text(setup._resource(setup.RULE_PATH.name))
    setup.install_udev()
    assert run.call_count == 2


def test_service_keeps_venv_path_and_quotes(environment, monkeypatch):
    root, _ = environment
    executable = root / 'virtual env % $' / 'bin/python'
    executable.parent.mkdir(parents=True)
    executable.symlink_to('/usr/bin/python3')
    monkeypatch.setattr(setup.sys, 'executable', str(executable))
    setup.install_service(start=False)
    unit = (root / 'systemd/user/ulanzi-niri.service').read_text()
    assert str(executable).replace('%', '%%').replace('$', '$$') in unit
    assert 'ExecStart="/usr/bin/python3"' not in unit


def test_default_config_home(environment, monkeypatch):
    root, _ = environment
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setattr(Path, "home", lambda: root)
    setup.install_config()
    assert load_config(root / ".config/ulanzi-niri/config.toml").page


@pytest.mark.parametrize("rule", [None, "# outdated\n", "current"])
def test_setup_checks_udev(environment, capsys, rule):
    if rule is not None:
        setup.RULE_PATH.parent.mkdir()
        setup.RULE_PATH.write_text(
            setup._resource(setup.RULE_PATH.name) if rule == "current" else rule
        )
    assert cli.main(["setup", "--no-config", "--no-service"]) == 0
    assert ("sudo ulanzi-niri admin-setup" in capsys.readouterr().err) == (rule != "current")


def test_admin_setup_requires_root(environment, capsys):
    root, run = environment
    assert cli.main(["admin-setup"]) == 1
    assert "sudo ulanzi-niri admin-setup" in capsys.readouterr().err
    assert not list(root.iterdir())
    run.assert_not_called()


def test_admin_setup_only_installs_udev(environment, monkeypatch):
    root, run = environment
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    assert cli.main(["admin-setup"]) == 0
    assert setup.udev_configured()
    assert setup.RULE_PATH.stat().st_mode & 0o777 == 0o644
    assert not (root / "ulanzi-niri").exists()
    assert not (root / "systemd").exists()
    assert [call.args[0] for call in run.call_args_list] == [
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=hidraw"],
    ]


def test_admin_setup_failure_can_be_retried(environment, monkeypatch, capsys):
    _, run = environment
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    run.side_effect = setup.subprocess.CalledProcessError(1, ["udevadm"])
    assert cli.main(["admin-setup"]) == 1
    assert "admin-setup failed" in capsys.readouterr().err
    run.reset_mock(side_effect=True)
    assert cli.main(["admin-setup"]) == 0
    assert run.call_count == 2


@pytest.mark.parametrize("missing", ["systemctl", "systemd"])
@pytest.mark.parametrize("flags", [[], ["--no-start"], ["--no-config"]])
def test_unsupported_systemd_is_skipped(environment, monkeypatch, capsys, missing, flags):
    root, run = environment
    if missing == "systemctl":
        monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    else:
        monkeypatch.setattr(setup, "SYSTEMD_RUNTIME", root / "no-systemd")
    assert cli.main(["setup", *flags]) == 0
    run.assert_not_called()
    assert not (root / "systemd").exists()
    assert (root / "ulanzi-niri/config.toml").exists() == ("--no-config" not in flags)
    warning = capsys.readouterr().err
    assert "warning: systemd is not available; skipping user service setup" in warning
    assert "ulanzi-niri run" in warning


def test_no_service_does_not_warn_about_systemd(environment, monkeypatch, capsys):
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    assert cli.main(["setup", "--no-service"]) == 0
    assert "systemd" not in capsys.readouterr().err
