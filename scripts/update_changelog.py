"""Update the changelog using git-changelog and the working-tree package version."""

import os
import subprocess
import tomllib
from pathlib import Path

from git_changelog import main as git_changelog


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    shallow = subprocess.check_output(
        ["git", "rev-parse", "--is-shallow-repository"], text=True,
    ).strip()
    if shallow == "true":
        raise SystemExit("Run git fetch --unshallow --tags before generating the changelog.")
    tags = subprocess.check_output(["git", "tag", "--merged", "HEAD"], text=True).splitlines()
    if not {"v1.0.0", "v2.0.0"}.issubset(tags):
        raise SystemExit("Fetch release tags first; v1.0.0 and v2.0.0 must be reachable.")
    with Path("pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    return git_changelog(["--jinja-context", f"package_version={version}"])


if __name__ == "__main__":
    raise SystemExit(main())
