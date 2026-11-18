"""Tag main as a release and push the tag, which starts the release workflow.

Dry run by default: it checks everything and prints the commands. Pass
`--run` to create the annotated tag and push it.

Checks: the working tree is clean, HEAD is origin/main after a fetch, the
version matches pyproject.toml, and the tag exists neither here nor on origin.

Usage: python scripts/release.py X.Y.Z [--run]
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from check_release import ROOT, pyproject_version, tag_error

REMOTE = "origin"
BRANCH = "main"

Runner = Callable[[list[str]], str]


@dataclass(frozen=True)
class State:
    """What the checks need to know about the repository."""

    dirty: bool
    head: str
    main: str
    local_tag: bool
    remote_tag: bool
    version: str


def git_runner(root: Path) -> Runner:
    def run(args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    return run


def read_state(git: Runner, tag: str, root: Path) -> State:
    git(["fetch", "--quiet", REMOTE, BRANCH])
    return State(
        dirty=bool(git(["status", "--porcelain"])),
        head=git(["rev-parse", "HEAD"]),
        main=git(["rev-parse", f"{REMOTE}/{BRANCH}"]),
        local_tag=bool(git(["tag", "--list", tag])),
        remote_tag=bool(git(["ls-remote", "--tags", REMOTE, f"refs/tags/{tag}"])),
        version=pyproject_version(root),
    )


def problems(tag: str, state: State) -> list[str]:
    found: list[str] = []
    if error := tag_error(tag, state.version):
        found.append(error)
    if state.dirty:
        found.append("the working tree has uncommitted changes")
    if state.head != state.main:
        found.append(
            f"HEAD {state.head[:12]} is not {REMOTE}/{BRANCH} {state.main[:12]}"
        )
    if state.local_tag:
        found.append(f"tag {tag} already exists locally")
    if state.remote_tag:
        found.append(f"tag {tag} already exists on {REMOTE}")
    return found


def commands(tag: str) -> list[list[str]]:
    return [
        ["tag", "--annotate", tag, "--message", f"sai-cli {tag}"],
        ["push", REMOTE, f"refs/tags/{tag}"],
    ]


def release(version: str, run: bool, git: Runner, root: Path = ROOT) -> int:
    tag = f"v{version}"
    found = problems(tag, read_state(git, tag, root))
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    if found:
        return 1
    for args in commands(tag):
        print(shlex.join(["git", *args]))
        if run:
            git(args)
    if run:
        print(f"Pushed {tag}; the release workflow builds and publishes it.")
    else:
        print("Dry run: nothing was tagged or pushed. Pass --run to release.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="X.Y.Z, matching pyproject.toml")
    parser.add_argument("--run", action="store_true", help="tag and push")
    args = parser.parse_args(argv)
    try:
        return release(args.version, args.run, git_runner(ROOT))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
