"""Fail a release before building if its stable SemVer tag or version is invalid."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def validate_release(tag: str, package_version: str) -> None:
    if TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(
            f"Invalid release tag {tag!r}: expected vMAJOR.MINOR.PATCH "
            "with no leading zeroes (for example v2.0.0)."
        )
    if tag[1:] != package_version:
        raise ValueError(
            f"Release tag {tag!r} does not match pyproject.toml version {package_version!r}. "
            "Update the package version and tag the commit containing that change."
        )


def main() -> int:
    try:
        with Path("pyproject.toml").open("rb") as stream:
            package_version = tomllib.load(stream)["project"]["version"]
        tag = os.environ.get("GITHUB_REF_NAME", "")
        validate_release(tag, package_version)
    except (OSError, ValueError, KeyError) as exc:
        # Escape workflow-command data so malformed input cannot inject annotations.
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    print(f"Release tag {tag} matches package version {package_version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
