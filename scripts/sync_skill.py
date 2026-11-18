"""Copy the `sai` agent skill from jiyuu into skills/sai/.

The skill is edited in https://github.com/Unique-Divine/jiyuu under
ai-skills/sai. This script replaces skills/sai/ with that directory as it is
at one jiyuu commit, and records the commit in skills/sai/.source.json.

Dry run by default: it prints the files that would change and the commit
message, which lists the jiyuu commits pulled since the last sync. Pass
`--run` to write the files, and `--commit` with it to commit them.

Usage:
  python scripts/sync_skill.py [--run] [--commit]
  python scripts/sync_skill.py --from ~/jiyuu --ref my-branch --run
  python scripts/sync_skill.py --url URL --ref main --run
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://github.com/Unique-Divine/jiyuu"
REF = "main"
SOURCE_PATH = "ai-skills/sai"
DEST = Path("skills") / "sai"
RECORD = ".source.json"


class SyncError(Exception):
    """A problem to report to the person; no traceback."""


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise SyncError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).decode().strip()


def clone(url: str, ref: str, into: Path) -> Path:
    """Clone REF of URL without a working tree. REF is a branch or tag."""
    result = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--bare",
            "--single-branch",
            "--branch",
            ref,
            url,
            str(into),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise SyncError(f"git clone {url} ({ref}) failed: {detail}")
    return into


def read_tree(repo: Path, commit: str) -> dict[str, bytes]:
    """Return the skill's files at COMMIT, keyed by path inside the skill."""
    try:
        archive = git(repo, "archive", "--format=tar", f"{commit}:{SOURCE_PATH}")
    except SyncError as exc:
        raise SyncError(f"{SOURCE_PATH} does not exist at {commit[:12]}") from exc
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise SyncError(f"{member.name} is not a regular file in jiyuu")
            data = tar.extractfile(member)
            assert data is not None
            files[member.name] = data.read()
    if not files.get("SKILL.md", b"").startswith(b"---"):
        raise SyncError(f"{SOURCE_PATH}/SKILL.md is missing or has no frontmatter")
    return files


def previous_commit(dest: Path) -> str | None:
    """The jiyuu commit of the last sync, or None before the first one."""
    record = dest / RECORD
    if not record.is_file():
        return None
    commit = json.loads(record.read_text(encoding="utf-8")).get("commit")
    return commit if isinstance(commit, str) else None


def has_commit(repo: Path, commit: str | None) -> bool:
    if commit is None:
        return False
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def pulled_commits(repo: Path, previous: str | None, commit: str) -> list[str]:
    """One line per jiyuu commit that touched the skill since PREVIOUS.

    Without a PREVIOUS that this history contains, return the latest 20.
    """
    if previous == commit:
        return []
    if has_commit(repo, previous):
        span, limit = f"{previous}..{commit}", []
    else:
        span, limit = commit, ["--max-count=20"]
    log = git_text(repo, "log", "--format=%h %s", *limit, span, "--", SOURCE_PATH)
    return log.splitlines()


def record_bytes(url: str, ref: str, commit: str) -> bytes:
    record = {"repository": url, "path": SOURCE_PATH, "ref": ref, "commit": commit}
    return (json.dumps(record, indent=2) + "\n").encode()


def current_files(dest: Path) -> dict[str, bytes]:
    if not dest.is_dir():
        return {}
    return {
        path.relative_to(dest).as_posix(): path.read_bytes()
        for path in sorted(dest.rglob("*"))
        if path.is_file()
    }


@dataclass(frozen=True)
class Plan:
    """What one sync would do."""

    commit: str
    previous: str | None
    files: dict[str, bytes]
    changes: list[str]
    message: str


def diff(old: dict[str, bytes], new: dict[str, bytes]) -> list[str]:
    lines = [f"add    {name}" for name in sorted(new.keys() - old.keys())]
    lines += [
        f"change {name}"
        for name in sorted(new.keys() & old.keys())
        if new[name] != old[name]
    ]
    lines += [f"remove {name}" for name in sorted(old.keys() - new.keys())]
    return lines


def commit_message(
    url: str, commit: str, previous: str | None, commits: list[str], known: bool
) -> str:
    lines = [
        f"skills: Sync sai from jiyuu {commit[:7]}",
        "",
        f"Source: {url} {SOURCE_PATH} at {commit}",
        f"Previous sync: {previous or 'none'}",
        "",
    ]
    if previous is None:
        lines.append("jiyuu commits (first sync, latest 20):")
    elif known:
        lines.append("jiyuu commits since the previous sync:")
    else:
        lines.append("jiyuu commits (previous sync not in this history, latest 20):")
    lines += [f"- {line}" for line in commits] or ["- none touched the skill"]
    return "\n".join(lines) + "\n"


def plan(repo: Path, ref: str, url: str, dest: Path) -> Plan:
    commit = git_text(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    previous = previous_commit(dest)
    commits = pulled_commits(repo, previous, commit)
    known = has_commit(repo, previous)
    files = read_tree(repo, commit)
    if RECORD in files:
        raise SyncError(f"jiyuu must not contain {SOURCE_PATH}/{RECORD}")
    files[RECORD] = record_bytes(url, ref, commit)
    return Plan(
        commit=commit,
        previous=previous,
        files=files,
        changes=diff(current_files(dest), files),
        message=commit_message(url, commit, previous, commits, known),
    )


def write(dest: Path, files: dict[str, bytes]) -> None:
    """Replace DEST with FILES."""
    if dest.is_symlink():
        raise SyncError(f"{dest} is a symlink; expected a directory")
    if dest.exists():
        shutil.rmtree(dest)
    for name, data in files.items():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def commit_sync(root: Path, message: str) -> None:
    git(root, "add", "--all", "--", DEST.as_posix())
    git(root, "commit", "--quiet", "--message", message, "--", DEST.as_posix())


def sync(
    repo: Path,
    ref: str,
    url: str,
    root: Path = ROOT,
    run: bool = False,
    commit: bool = False,
) -> int:
    dest = root / DEST
    planned = plan(repo, ref, url, dest)
    if not planned.changes:
        print(f"skills/sai already matches jiyuu {planned.commit[:12]}.")
        return 0
    for line in planned.changes:
        print(line)
    print()
    print(planned.message, end="")
    if not run:
        print("\nDry run: nothing was written. Pass --run to sync.")
        return 0
    write(dest, planned.files)
    print(f"\nWrote {len(planned.files)} files to {DEST.as_posix()}.")
    if commit:
        commit_sync(root, planned.message)
        print("Committed.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--from",
        dest="checkout",
        type=Path,
        help="a local jiyuu checkout to read instead of cloning --url",
    )
    parser.add_argument("--url", default=URL, help=f"jiyuu Git URL (default {URL})")
    parser.add_argument(
        "--ref",
        help=f"branch or tag to sync (default {REF}; HEAD with --from)",
    )
    parser.add_argument("--run", action="store_true", help="write skills/sai")
    parser.add_argument(
        "--commit", action="store_true", help="with --run, commit skills/sai"
    )
    args = parser.parse_args(argv)
    if args.commit and not args.run:
        parser.error("--commit needs --run")
    try:
        if args.checkout is not None:
            checkout = args.checkout.expanduser()
            ref = args.ref or "HEAD"
            return sync(checkout, ref, args.url, ROOT, args.run, args.commit)
        with tempfile.TemporaryDirectory() as tmp:
            ref = args.ref or REF
            repo = clone(args.url, ref, Path(tmp) / "jiyuu.git")
            return sync(repo, ref, args.url, ROOT, args.run, args.commit)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
