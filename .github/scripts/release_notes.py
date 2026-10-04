"""Print the CHANGELOG.md section for a release tag as GitHub Release notes."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

COMPARE_LINK = re.compile(r"\]\((?P<base>\S+?/compare/\S+?)\.\.\.\S+?\)")


def release_notes(changelog: str, tag: str) -> str:
    heading = f"## [{tag.removeprefix('v')}]"
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        raise ValueError(f"CHANGELOG.md has no {heading} section for release tag {tag!r}.")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    notes = "\n".join(lines[start + 1 : end]).strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md section {heading} for release tag {tag!r} is empty.")
    # The section is generated before tagging, so its link compares against HEAD.
    link = COMPARE_LINK.search(lines[start])
    if link is None:
        raise ValueError(f"CHANGELOG.md section {heading} has no compare link.")
    return f"{notes}\n\n**Full Changelog**: {link['base']}...{tag}\n"


def main() -> int:
    tag = os.environ.get("GITHUB_REF_NAME", "")
    try:
        notes = release_notes(Path("CHANGELOG.md").read_text(), tag)
    except (OSError, ValueError) as exc:
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
