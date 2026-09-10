"""Test project-specific git-changelog configuration across a release cycle."""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_changelog_release_cycle(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    def generate(version):
        project = (ROOT / "pyproject.toml").read_text()
        current_version = tomllib.loads(project)["project"]["version"]
        project = project.replace(f'version = "{current_version}"', f'version = "{version}"', 1)
        (tmp_path / "pyproject.toml").write_text(project)
        subprocess.run(
            [sys.executable, str(tmp_path / "scripts/update_changelog.py")],
            check=True, capture_output=True,
        )
        return (tmp_path / "CHANGELOG.md").read_text()

    for file in ("scripts/update_changelog.py", "config/changelog.md.jinja"):
        (tmp_path / file).parent.mkdir(exist_ok=True)
        shutil.copy(ROOT / file, tmp_path / file)
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    git("remote", "add", "origin", "https://github.com/doitian/ulanzi-studio-niri.git")
    git("commit", "--allow-empty", "-m", "feat: old feature")
    git("tag", "v1.0.0")
    git("commit", "--allow-empty", "-m", "feat: another old feature")
    git("tag", "v2.0.0")
    assert "## [Unreleased]" in generate("2.0.0")  # HEAD is exactly the tag.
    git("commit", "--allow-empty", "-m", "feat: support another controller")
    git("commit", "--allow-empty", "-m", "fix!: restore connection")
    git("commit", "--allow-empty", "-m", "chore: release bookkeeping")
    text = generate("2.0.0")
    assert "### Added\n\n- support another controller" in text
    assert "### Fixed\n\n- **Breaking:** restore connection" in text
    assert "old feature" not in text
    assert "release bookkeeping" not in text
    assert "###" not in text.split("## [2.0.0]")[1]
    assert generate("2.0.0") == text

    upcoming = generate("2.1.0")
    assert "## [2.1.0]" in upcoming
    assert ") - Unreleased\n\n### Added" in upcoming
    assert "###" not in upcoming.split("## [2.1.0]")[0]
    git("tag", "-a", "v2.1.0", "-m", "Release 2.1.0")
    git("commit", "--allow-empty", "-m", "docs: explain new controller")
    released = generate("2.1.0")
    assert ") - Unreleased" not in released
    assert "explain new controller" in released.split("## [2.1.0]")[0]
    assert "support another controller" in released.split("## [2.1.0]")[1]
    assert "compare/v2.0.0...v2.1.0" in released
    assert "compare/v2.1.0...HEAD" in released
