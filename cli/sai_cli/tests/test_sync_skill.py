"""scripts/sync_skill.py against two throwaway Git repositories."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_skill

URL = "https://example.com/jiyuu"
SKILL = "---\nname: sai\ndescription: Test skill.\n---\n\n# Sai\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def init(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    return path


def commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    """Write FILES (None deletes), commit them, and return the commit."""
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def jiyuu(tmp_path) -> Path:
    repo = init(tmp_path / "jiyuu")
    commit(
        repo,
        "skills: Add sai",
        {
            "ai-skills/sai/SKILL.md": SKILL,
            "ai-skills/sai/cli.md": "# CLI\n",
            "ai-skills/sai/references/rest.md": "# REST\n",
            "ai-skills/other/SKILL.md": "---\nname: other\n---\n",
        },
    )
    return repo


@pytest.fixture
def open_sai(tmp_path) -> Path:
    repo = init(tmp_path / "open-sai")
    commit(
        repo,
        "init",
        {"skills/sai/SKILL.md": "---\nname: sai\n---\nold\n", "README.md": "x\n"},
    )
    return repo


def run(jiyuu: Path, open_sai: Path, **flags: bool) -> int:
    return sync_skill.sync(jiyuu, "HEAD", URL, open_sai, **flags)


def tree(root: Path) -> set[str]:
    dest = root / "skills" / "sai"
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}


def test_dry_run_prints_the_plan_and_writes_nothing(jiyuu, open_sai, capsys):
    assert run(jiyuu, open_sai) == 0
    out = capsys.readouterr().out
    assert "add    cli.md" in out
    assert "change SKILL.md" in out
    assert "skills: Sync sai from jiyuu" in out
    assert "Dry run" in out
    assert tree(open_sai) == {"SKILL.md"}
    assert git(open_sai, "status", "--porcelain") == ""


def test_run_replaces_the_directory_and_records_the_commit(jiyuu, open_sai):
    stale = open_sai / "skills" / "sai" / "stale.md"
    stale.write_text("gone after sync\n")
    assert run(jiyuu, open_sai, run=True) == 0
    assert tree(open_sai) == {
        "SKILL.md",
        "cli.md",
        "references/rest.md",
        ".source.json",
    }
    dest = open_sai / "skills" / "sai"
    assert (dest / "SKILL.md").read_text() == SKILL
    assert json.loads((dest / ".source.json").read_text()) == {
        "repository": URL,
        "path": "ai-skills/sai",
        "ref": "HEAD",
        "commit": git(jiyuu, "rev-parse", "HEAD"),
    }
    # Only the skill directory changed, and nothing was committed.
    assert git(open_sai, "log", "--oneline").count("\n") == 0


def test_commit_lists_jiyuu_commits_since_the_last_sync(jiyuu, open_sai, capsys):
    assert run(jiyuu, open_sai, run=True, commit=True) == 0
    first = git(open_sai, "log", "-1", "--format=%B")
    assert "Previous sync: none" in first
    assert "skills: Add sai" in first

    commit(jiyuu, "skills: Explain doctor", {"ai-skills/sai/cli.md": "# CLI\ndoc\n"})
    commit(jiyuu, "other: Unrelated", {"ai-skills/other/SKILL.md": "changed\n"})
    head = commit(
        jiyuu,
        "skills: Drop REST page",
        {"ai-skills/sai/references/rest.md": None},
    )
    (open_sai / "untracked.txt").write_text("left alone\n")
    capsys.readouterr()
    assert run(jiyuu, open_sai, run=True, commit=True) == 0
    out = capsys.readouterr().out
    assert "remove references/rest.md" in out

    message = git(open_sai, "log", "-1", "--format=%B")
    assert message.startswith(f"skills: Sync sai from jiyuu {head[:7]}\n")
    assert "jiyuu commits since the previous sync:" in message
    assert "skills: Explain doctor" in message
    assert "skills: Drop REST page" in message
    assert "Unrelated" not in message
    assert "skills: Add sai" not in message
    assert tree(open_sai) == {"SKILL.md", "cli.md", ".source.json"}
    assert git(open_sai, "status", "--porcelain") == "?? untracked.txt"


def test_second_sync_of_the_same_commit_does_nothing(jiyuu, open_sai, capsys):
    assert run(jiyuu, open_sai, run=True, commit=True) == 0
    before = git(open_sai, "rev-parse", "HEAD")
    capsys.readouterr()
    assert run(jiyuu, open_sai, run=True, commit=True) == 0
    assert "already matches" in capsys.readouterr().out
    assert git(open_sai, "rev-parse", "HEAD") == before


def test_unknown_previous_commit_falls_back_to_recent_history(jiyuu, open_sai):
    record = open_sai / "skills" / "sai" / ".source.json"
    record.write_text(json.dumps({"commit": "0" * 40}))
    planned = sync_skill.plan(jiyuu, "HEAD", URL, open_sai / "skills" / "sai")
    assert "previous sync not in this history" in planned.message
    assert "skills: Add sai" in planned.message


def test_syncs_a_branch_from_a_url(jiyuu, open_sai, tmp_path, monkeypatch, capsys):
    git(jiyuu, "checkout", "-q", "-b", "next")
    head = commit(jiyuu, "skills: Next", {"ai-skills/sai/mcp.md": "# MCP\n"})
    git(jiyuu, "checkout", "-q", "main")
    monkeypatch.setattr(sync_skill, "ROOT", open_sai)
    assert sync_skill.main(["--url", str(jiyuu), "--ref", "next", "--run"]) == 0
    record = json.loads((open_sai / "skills/sai/.source.json").read_text())
    assert record["commit"] == head
    assert record["ref"] == "next"
    assert "mcp.md" in tree(open_sai)


def test_refuses_a_source_without_the_skill(jiyuu, open_sai, tmp_path, capsys):
    empty = init(tmp_path / "empty")
    commit(empty, "init", {"README.md": "x\n"})
    with pytest.raises(sync_skill.SyncError, match="does not exist"):
        run(empty, open_sai, run=True)
    assert tree(open_sai) == {"SKILL.md"}

    commit(jiyuu, "break", {"ai-skills/sai/SKILL.md": "no frontmatter\n"})
    with pytest.raises(sync_skill.SyncError, match="frontmatter"):
        run(jiyuu, open_sai, run=True)
    assert tree(open_sai) == {"SKILL.md"}


def test_commit_flag_needs_run(capsys):
    with pytest.raises(SystemExit):
        sync_skill.main(["--commit"])
    assert "--commit needs --run" in capsys.readouterr().err
