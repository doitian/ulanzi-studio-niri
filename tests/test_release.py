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


NOTES_SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/release_notes.py"
notes_spec = importlib.util.spec_from_file_location("release_notes", NOTES_SCRIPT)
assert notes_spec is not None and notes_spec.loader is not None
notes = importlib.util.module_from_spec(notes_spec)
notes_spec.loader.exec_module(notes)

CHANGELOG = """# Changelog

## [Unreleased](https://example.invalid/compare/v2.1.0...HEAD)

## [2.1.0](https://example.invalid/compare/v2.0.0...HEAD) - Unreleased

### Added

- new feature.

## [2.0.0](https://example.invalid/compare/v1.0.0...v2.0.0) - 2026-09-10

## [1.0.0](https://example.invalid/compare/abc...v1.0.0) - 2026-05-06
"""


def test_release_notes_extracts_version_section():
    assert notes.release_notes(CHANGELOG, "v2.1.0") == (
        "### Added\n\n- new feature.\n\n"
        "**Full Changelog**: https://example.invalid/compare/v2.0.0...v2.1.0\n"
    )


def test_release_notes_requires_compare_link():
    changelog = CHANGELOG.replace("(https://example.invalid/compare/v2.0.0...HEAD)", "")
    with pytest.raises(ValueError, match="no compare link"):
        notes.release_notes(changelog, "v2.1.0")


def test_release_notes_requires_section():
    with pytest.raises(ValueError, match=r"no ## \[2.2.0\] section"):
        notes.release_notes(CHANGELOG, "v2.2.0")


def test_release_notes_rejects_empty_section():
    with pytest.raises(ValueError, match="is empty"):
        notes.release_notes(CHANGELOG, "v2.0.0")


def test_release_notes_does_not_match_version_prefix():
    with pytest.raises(ValueError, match="no"):
        notes.release_notes(CHANGELOG, "v2.1")


def test_release_notes_main_reports_missing_section(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    monkeypatch.setenv("GITHUB_REF_NAME", "v9.9.9")
    assert notes.main() == 1
    assert "::error::CHANGELOG.md has no ## [9.9.9] section" in capsys.readouterr().err
