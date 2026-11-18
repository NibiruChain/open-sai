"""scripts/check_release.py and scripts/release.py against real Git repos."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_release
import release


def test_tag_error():
    assert check_release.tag_for("0.1.0") == "sai-cli/v0.1.0"
    assert check_release.tag_error("sai-cli/v0.1.0", "0.1.0") is None
    assert check_release.tag_error("sai-cli/v0.2.0-rc.1", "0.2.0-rc.1") is None
    assert "does not match" in str(check_release.tag_error("sai-cli/v0.2.0", "0.1.0"))
    bad = (
        "v0.1.0",
        "0.1.0",
        "sai-cli/0.1.0",
        "sai-cli/v0.1",
        "sai-cli/v0.1.0-beta",
        "sai-cli/v0.1.0rc1",
        "sai-rs/v0.1.0",
        "x/sai-cli/v0.1.0",
    )
    for tag in bad:
        assert "is not" in str(check_release.tag_error(tag, "0.1.0")), tag


def test_check_release_matches_this_pyproject():
    version = check_release.pyproject_version()
    assert check_release.main([f"sai-cli/v{version}"]) == 0
    assert check_release.main(["sai-cli/v0.0.0-rc.0"]) == 1
    assert check_release.main([f"v{version}"]) == 1
    assert check_release.main([]) == 2


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A clone of a bare origin whose main declares version 1.2.3."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    git(work, "config", "commit.gpgsign", "false")
    git(work, "config", "tag.gpgsign", "false")
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    return work


def run(repo: Path, version: str, go: bool = False) -> int:
    return release.release(version, go, release.git_runner(repo), repo)


def test_dry_run_checks_and_changes_nothing(repo, capsys):
    assert run(repo, "1.2.3") == 0
    out = capsys.readouterr().out
    assert "git tag --annotate sai-cli/v1.2.3" in out
    assert "git push origin refs/tags/sai-cli/v1.2.3" in out
    assert "Dry run" in out
    assert git(repo, "tag", "--list") == ""
    assert git(repo, "ls-remote", "--tags", "origin") == ""


def test_run_tags_and_pushes(repo):
    assert run(repo, "1.2.3", go=True) == 0
    assert git(repo, "cat-file", "-t", "sai-cli/v1.2.3") == "tag"
    assert "refs/tags/sai-cli/v1.2.3" in git(repo, "ls-remote", "--tags", "origin")
    # A second release of the same version is refused.
    assert run(repo, "1.2.3") == 1


def test_refuses_version_mismatch_dirty_tree_and_unpushed_head(repo, capsys):
    assert run(repo, "1.2.4", go=True) == 1
    assert "does not match" in capsys.readouterr().err

    (repo / "stray.txt").write_text("x")
    assert run(repo, "1.2.3", go=True) == 1
    assert "uncommitted" in capsys.readouterr().err

    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "local only")
    assert run(repo, "1.2.3", go=True) == 1
    assert "is not origin/main" in capsys.readouterr().err
    assert git(repo, "tag", "--list") == ""
