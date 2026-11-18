"""The agent skill packaged with the CLI: read it, locate it, install it.

The skill is a directory: SKILL.md, child pages, and `.source.json`, which
records the jiyuu commit it was copied from. `cli/sai_cli/skills` is a
symlink to `skills/sai` in the repository, so wheels and sdists carry the
real files.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import click

NAME = "sai"
RECORD = ".source.json"
CLI_REPOSITORY = "https://github.com/SaiDotFun/open-sai"
SKILL_REPOSITORY = "https://github.com/Unique-Divine/jiyuu"
NPX_INSTALL = "npx skills add SaiDotFun/open-sai --skill sai"


def check_skill(data: bytes) -> bytes:
    """Reject a packaged skill that is not a skill file.

    The package copy is a symlink to skills/sai. A checkout without symlink
    support, such as Windows without core.symlinks, stores the link target
    path as file text instead.
    """
    if not data.startswith(b"---"):
        raise click.ClickException(
            "Packaged SKILL.md has no frontmatter; this build may contain a"
            " symlink path instead of the skill. Reinstall from a checkout"
            " with symlinks enabled."
        )
    return data


def packaged_root() -> Traversable:
    return resources.files("sai_cli").joinpath("skills")


def walk(root: Traversable, prefix: str = "") -> dict[str, bytes]:
    """Return every file under ROOT, keyed by its POSIX path inside it."""
    files: dict[str, bytes] = {}
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name == "__pycache__":
            continue
        name = f"{prefix}{entry.name}"
        if entry.is_dir():
            files.update(walk(entry, f"{name}/"))
        elif entry.is_file():
            files[name] = entry.read_bytes()
    return files


def read_packaged_files() -> dict[str, bytes]:
    root = packaged_root()
    if not root.is_dir():
        # A checkout without symlinks has the link target as a text file.
        raise click.ClickException(
            "The packaged skill is not a directory; this build may contain a"
            " symlink path instead of the skill. Reinstall from a checkout"
            " with symlinks enabled."
        )
    return walk(root)


def packaged_files() -> dict[str, bytes]:
    """Return the packaged skill directory, checked to hold a real skill."""
    files = read_packaged_files()
    check_skill(files.get("SKILL.md", b""))
    return files


def skill_bytes() -> bytes:
    """Return the SKILL.md packaged with this version of the CLI."""
    return packaged_files()["SKILL.md"]


def pages(files: dict[str, bytes]) -> list[str]:
    """Page names `--page` accepts: Markdown paths without the suffix."""
    return [name[:-3] for name in files if name.endswith(".md") and name != "SKILL.md"]


def find_page(files: dict[str, bytes], name: str) -> bytes:
    """Return the page called NAME: `cli`, `cli.md`, `references/rest`, or a
    file name that only one page has, such as `rest`."""
    wanted = name.removesuffix(".md")
    if wanted == "SKILL":
        return files["SKILL.md"]
    known = pages(files)
    matches = [page for page in known if page == wanted]
    if not matches:
        matches = [page for page in known if page.rsplit("/", 1)[-1] == wanted]
    if len(matches) != 1:
        problem = "is ambiguous" if matches else "does not exist"
        raise click.ClickException(
            f"Skill page {name!r} {problem}. Pages: {', '.join(known)}"
        )
    return files[f"{matches[0]}.md"]


def source_record(files: dict[str, bytes]) -> dict[str, Any]:
    """The jiyuu commit this copy was synced from, or {} when unrecorded."""
    try:
        record = json.loads(files.get(RECORD, b"{}"))
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def default_dir() -> Path:
    return Path.home() / ".agents" / "skills" / NAME


def source_info(files: dict[str, bytes], version: str) -> dict[str, Any]:
    record = source_record(files)
    return {
        "name": NAME,
        "cliVersion": version,
        "path": str(packaged_root()),
        "pages": pages(files),
        "repositories": {"cli": CLI_REPOSITORY, "skill": SKILL_REPOSITORY},
        "source": {
            "repository": record.get("repository", SKILL_REPOSITORY),
            "path": record.get("path"),
            "ref": record.get("ref"),
            "commit": record.get("commit"),
        },
        "install": {
            "sai": "sai skill install",
            "npx": NPX_INSTALL,
            "defaultDir": str(default_dir()),
        },
    }


def render_source(info: dict[str, Any]) -> str:
    source = info["source"]
    commit = source["commit"] or "not recorded"
    return "\n".join(
        [
            f"Skill:      {info['name']} (packaged with sai {info['cliVersion']})",
            f"Path:       {info['path']}",
            f"Pages:      {', '.join(info['pages'])}",
            f"CLI repo:   {info['repositories']['cli']}",
            f"Skill repo: {info['repositories']['skill']}",
            f"Synced at:  {commit}",
            "Install:",
            f"  {info['install']['sai']}    (copies it to {info['install']['defaultDir']})",
            f"  {info['install']['npx']}",
        ]
    )


@dataclass(frozen=True)
class Installed:
    """What `install` did."""

    action: str
    dir: Path
    files: int
    commit: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "dir": str(self.dir),
            "files": self.files,
            "commit": self.commit,
        }


def existing_files(target: Path) -> dict[str, bytes]:
    return {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }


def is_sai_skill(existing: dict[str, bytes]) -> bool:
    """True when a directory's SKILL.md names the `sai` skill."""
    head = existing.get("SKILL.md", b"")[:200].replace(b"\r\n", b"\n")
    return head.startswith(b"---\nname: sai\n")


def write_dir(target: Path, files: dict[str, bytes]) -> None:
    """Write FILES to a new sibling directory, then move it to TARGET."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        for name, data in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        staging.chmod(0o755)
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install(files: dict[str, bytes], target: Path, force: bool = False) -> Installed:
    """Copy the packaged skill to TARGET.

    Without FORCE, refuse to replace a symlink or a directory whose content
    differs. Even with FORCE, refuse a directory that is not a `sai` skill,
    so a mistyped --dir cannot delete something else.
    """
    commit = source_record(files).get("commit")

    def done(action: str) -> Installed:
        return Installed(action, target, len(files), commit)

    if target.is_symlink():
        if not force:
            raise click.ClickException(
                f"{target} is a symlink to {os.readlink(target)}, so another"
                " tool manages it. Pass --force to replace the link with a"
                " copy; the link's target is left untouched."
            )
        write_dir(target, files)
        return done("replaced")
    if not target.exists():
        write_dir(target, files)
        return done("installed")
    if not target.is_dir():
        raise click.ClickException(f"{target} exists and is not a directory.")
    existing = existing_files(target)
    if not existing:
        write_dir(target, files)
        return done("installed")
    if existing == files:
        return done("unchanged")
    if not is_sai_skill(existing):
        raise click.ClickException(
            f"{target} holds something other than the sai skill; nothing was"
            " changed. Choose another --dir, or remove it yourself."
        )
    if not force:
        changed = sorted(
            name
            for name in existing.keys() | files.keys()
            if existing.get(name) != files.get(name)
        )
        shown = ", ".join(changed[:5]) + (", ..." if len(changed) > 5 else "")
        raise click.ClickException(
            f"{target} differs from the packaged skill in {len(changed)}"
            f" file(s): {shown}. Pass --force to replace the whole directory."
        )
    write_dir(target, files)
    return done("replaced")


ACTIONS = {
    "installed": "Installed the sai skill",
    "replaced": "Replaced the sai skill",
    "unchanged": "The sai skill is already up to date",
}


def render_installed(result: Installed) -> str:
    where = f"{ACTIONS[result.action]} at {result.dir} ({result.files} files"
    synced = f", jiyuu {result.commit[:12]}" if result.commit else ""
    return f"{where}{synced})."
