"""Build the wheel and sdist and check that both ship the real agent skill.

The package copy `cli/sai_cli/skills` is a symlink to `skills/sai`. This
script fails if a built archive holds a link instead of the files' bytes,
misses a file of the skill directory, or carries one that differs.

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
SKILL = ROOT / "skills" / "sai"
MEMBER = "sai_cli/skills/"


def source_files(skill: Path = SKILL) -> dict[str, bytes]:
    """Return the skill directory's files, keyed by path inside it."""
    return {
        path.relative_to(skill).as_posix(): path.read_bytes()
        for path in sorted(skill.rglob("*"))
        if path.is_file()
    }


def wheel_skill(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.startswith(MEMBER) or info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"{path.name}: {info.filename} is a symlink")
            files[info.filename[len(MEMBER) :]] = archive.read(info)
    return files


def sdist_skill(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            _, found, name = member.name.partition(f"/cli/{MEMBER}")
            if member.name.endswith(f"/cli/{MEMBER.rstrip('/')}"):
                found, name = "yes", ""
            if not found or member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"{path.name}: {member.name} is not a regular file")
            data = archive.extractfile(member)
            assert data is not None
            files[name] = data.read()
    return files


def compare(
    name: str, shipped: dict[str, bytes], expected: dict[str, bytes]
) -> list[str]:
    errors = [
        f"{name}: {MEMBER}{missing} is missing"
        for missing in sorted(expected.keys() - shipped.keys())
    ]
    errors += [
        f"{name}: {MEMBER}{extra} is not in skills/sai"
        for extra in sorted(shipped.keys() - expected.keys())
    ]
    errors += [
        f"{name}: {MEMBER}{changed} differs from skills/sai"
        for changed in sorted(shipped.keys() & expected.keys())
        if shipped[changed] != expected[changed]
    ]
    return errors


def check(out_dir: Path, expected: dict[str, bytes]) -> list[str]:
    """Return one error per skill file an archive ships wrongly."""
    errors: list[str] = []
    archives = [*out_dir.glob("*.whl"), *out_dir.glob("*.tar.gz")]
    if len(archives) != 2:
        return [f"expected one wheel and one sdist, found {archives}"]
    for archive in archives:
        reader = wheel_skill if archive.suffix == ".whl" else sdist_skill
        try:
            errors += compare(archive.name, reader(archive), expected)
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def main() -> int:
    expected = source_files()
    if not expected.get("SKILL.md", b"").startswith(b"---"):
        print("error: skills/sai/SKILL.md is missing or has no frontmatter")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        subprocess.run(
            ["uv", "build", "--quiet", "--out-dir", str(out_dir)],
            cwd=ROOT,
            check=True,
        )
        errors = check(out_dir, expected)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if not errors:
        print(f"wheel and sdist ship all {len(expected)} files of skills/sai")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
