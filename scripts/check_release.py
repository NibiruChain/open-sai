"""Check that a release tag names the version in pyproject.toml.

The release workflow runs this before it builds anything, so a tag pushed
without its version bump fails instead of publishing a mislabeled release.

Usage: python scripts/check_release.py vX.Y.Z
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# vMAJOR.MINOR.PATCH, or a pre-release such as v0.2.0-rc.1.
TAG = re.compile(r"^v\d+\.\d+\.\d+(-rc\.\d+)?$")


def pyproject_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def tag_error(tag: str, version: str) -> str | None:
    """Return why TAG cannot release VERSION, or None when it can."""
    if not TAG.match(tag):
        return f"tag {tag!r} is not vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N"
    if tag != f"v{version}":
        return f"tag {tag} does not match pyproject.toml version {version}"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_release.py vX.Y.Z", file=sys.stderr)
        return 2
    tag = argv[0]
    error = tag_error(tag, pyproject_version())
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{tag} matches pyproject.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
