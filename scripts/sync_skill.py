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
import os
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


def check_name(name: str) -> None:
    """Refuse a path that could land outside skills/sai on any platform.

    Git paths use `/`. A backslash or a drive colon is ordinary text on Unix
    and path syntax on Windows, so both are refused everywhere.
    """
    parts = name.split("/")
    unsafe = (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\0" in name
        or any(part in ("", ".", "..") for part in parts)
    )
    if unsafe:
        raise SyncError(f"refusing unsafe path in jiyuu: {name!r}")


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
            check_name(member.name)
            data = tar.extractfile(member)
            assert data is not None
            files[member.name] = data.read()
    if not files.get("SKILL.md", b"").startswith(b"---"):
        raise SyncError(f"{SOURCE_PATH}/SKILL.md is missing or has no frontmatter")
    return files


def record_commit(text: str) -> str | None:
    try:
        commit = json.loads(text).get("commit")
    except (ValueError, AttributeError):
        return None
    return commit if isinstance(commit, str) else None


def previous_commit(root: Path) -> str | None:
    """The jiyuu commit of the last sync, or None before the first one.

    In a Git repository the last committed sync counts, read from HEAD, so a
    sync already written by `--run` still lists its commits when committed.
    Outside one, the record in the working tree.
    """
    record = (DEST / RECORD).as_posix()
    in_repo = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"],
        capture_output=True,
        check=False,
    )
    if in_repo.returncode == 0:
        head = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{record}"],
            capture_output=True,
            check=False,
        )
        if head.returncode != 0:
            return None
        return record_commit(head.stdout.decode(errors="replace"))
    path = root / DEST / RECORD
    if not path.is_file():
        return None
    return record_commit(path.read_text(encoding="utf-8"))


def pending(root: Path) -> bool:
    """True when skills/sai differs from HEAD or holds untracked files."""
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", DEST.as_posix()],
        capture_output=True,
        check=False,
    )
    return status.returncode == 0 and bool(status.stdout.strip())


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


def plan(repo: Path, ref: str, url: str, root: Path) -> Plan:
    dest = root / DEST
    commit = git_text(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    previous = previous_commit(root)
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
    """Replace DEST with FILES.

    FILES is written to a fresh directory beside DEST under the literal paths
    from jiyuu, so a symlink in the old tree cannot redirect a write. The old
    tree is moved aside, the new one moved in, and the old one deleted last;
    if the swap stops, the old tree is moved back.
    """
    if dest.is_symlink():
        raise SyncError(f"{dest} is a symlink; expected a directory")
    for name in files:
        check_name(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}-new-", dir=dest.parent))
    backup = dest.parent / f".{dest.name}-old-{staging.name[-8:]}"
    try:
        base = staging.resolve()
        for name, data in files.items():
            target = base.joinpath(*name.split("/"))
            if base not in target.resolve().parents:
                raise SyncError(f"refusing to write outside {dest}: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        staging.chmod(0o755)
        if dest.exists():
            os.replace(dest, backup)
        os.replace(staging, dest)
    except BaseException:
        if backup.exists() and not dest.exists():
            os.replace(backup, dest)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # rmtree removes links inside the old tree without following them.
    shutil.rmtree(backup, ignore_errors=True)


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
    planned = plan(repo, ref, url, root)
    # A sync written earlier by --run, or one whose commit failed, leaves
    # the files in place and uncommitted; --commit still has work to do.
    uncommitted = commit and pending(root)
    if not planned.changes and not uncommitted:
        print(f"skills/sai already matches jiyuu {planned.commit[:12]}.")
        return 0
    for line in planned.changes:
        print(line)
    if not planned.changes:
        print(f"skills/sai already holds jiyuu {planned.commit[:12]}, uncommitted.")
    print()
    print(planned.message, end="")
    if not run:
        print("\nDry run: nothing was written. Pass --run to sync.")
        return 0
    if planned.changes:
        write(root / DEST, planned.files)
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
