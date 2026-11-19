"""Package every file of the agent skill, at any depth.

`cli/sai_cli/skills` is a symlink to `skills/sai`. setuptools globs skip
hidden directories, so this walks the tree and lists each file. Everything
else about the package is in pyproject.toml.
"""

from __future__ import annotations

import os
from pathlib import Path

from setuptools import setup

PACKAGE = Path(__file__).resolve().parent / "cli" / "sai_cli"


def skill_files(package: Path = PACKAGE) -> list[str]:
    """Return each file under PACKAGE/skills as a path relative to PACKAGE,
    hidden files and directories included."""
    found: list[str] = []
    for parent, _dirs, names in os.walk(package / "skills", followlinks=True):
        for name in names:
            found.append((Path(parent) / name).relative_to(package).as_posix())
    return sorted(found)


if __name__ == "__main__":
    setup(package_data={"sai_cli": skill_files()})
