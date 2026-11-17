"""Build the wheel and sdist and check that both ship the real agent skill.

The package copy of SKILL.md is a symlink to skills/sai/SKILL.md. This script
fails if a built archive contains the link instead of the file's bytes.

Usage: python scripts/check_dist.py
"""

from __future__ import annotations

import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "sai" / "SKILL.md"
MEMBER = "sai_cli/skills/SKILL.md"


def wheel_skill(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(MEMBER)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"{path.name}: {MEMBER} is a symlink")
        return archive.read(info)


def sdist_skill(path: Path) -> bytes:
    with tarfile.open(path) as archive:
        matches = [m for m in archive.getmembers() if m.name.endswith(MEMBER)]
        if len(matches) != 1:
            raise ValueError(f"{path.name}: expected one {MEMBER}, found {matches}")
        member = matches[0]
        if not member.isfile():
            raise ValueError(f"{path.name}: {member.name} is not a regular file")
        data = archive.extractfile(member)
        assert data is not None
        return data.read()


def check(out_dir: Path, expected: bytes) -> list[str]:
    """Return one error per archive whose skill differs from the source."""
    errors: list[str] = []
    archives = [*out_dir.glob("*.whl"), *out_dir.glob("*.tar.gz")]
    if len(archives) != 2:
        return [f"expected one wheel and one sdist, found {archives}"]
    for archive in archives:
        reader = wheel_skill if archive.suffix == ".whl" else sdist_skill
        try:
            if reader(archive) != expected:
                errors.append(f"{archive.name}: {MEMBER} differs from {SKILL}")
        except (KeyError, ValueError) as exc:
            errors.append(str(exc))
    return errors


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        subprocess.run(
            ["uv", "build", "--quiet", "--out-dir", str(out_dir)],
            cwd=ROOT,
            check=True,
        )
        errors = check(out_dir, SKILL.read_bytes())
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if not errors:
        print("wheel and sdist ship skills/sai/SKILL.md")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
