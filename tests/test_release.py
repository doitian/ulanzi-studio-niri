"""Exercise the release gate used before any publish workflow jobs."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/validate_release.py"
spec = importlib.util.spec_from_file_location("validate_release", SCRIPT)
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("version", ["0.0.0", "2.0.0", "12.34.567"])
def test_matching_release(version):
    release.validate_release(f"v{version}", version)


@pytest.mark.parametrize("tag", [
    "", "2.0.0", "V2.0.0", "v2", "v2.0", "v2.0.0.1", "v02.0.0", "v2.00.0",
    "v2.0.00", "v2.0.0-rc.1", "v2.0.0+build", "v2.0.0\n", "version-2.0.0",
])
def test_invalid_release_tag(tag):
    with pytest.raises(ValueError, match="Invalid release tag"):
        release.validate_release(tag, "2.0.0")


def test_mismatched_version():
    with pytest.raises(ValueError, match="does not match"):
        release.validate_release("v2.1.0", "2.0.0")


def test_main_reports_mismatch_as_release_error(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    monkeypatch.setenv("GITHUB_REF_NAME", "v2.1.0")
    assert release.main() == 1
    assert "::error::Release tag 'v2.1.0' does not match" in capsys.readouterr().err


def test_main_accepts_matching_version(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    monkeypatch.setenv("GITHUB_REF_NAME", "v2.0.0")
    assert release.main() == 0
