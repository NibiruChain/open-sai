"""Tests for the packaged agent skill and version flags."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from click.testing import CliRunner
from sai_cli import skill
from sai_cli.sai_cli import main

REPO_SKILL = Path(__file__).parents[3] / "skills" / "sai" / "SKILL.md"


def _cli() -> list[str]:
    """Run this environment's `sai`, never another one found on PATH."""
    script = Path(sys.executable).parent / ("sai.exe" if os.name == "nt" else "sai")
    return [str(script)] if script.exists() else [sys.executable, "-m", "sai_cli"]


def test_skill_prints_repo_skill_without_sai_home(tmp_path):
    sai_home = tmp_path / "sai-home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAI_")}
    env["SAI_HOME"] = str(sai_home)
    result = subprocess.run(
        [*_cli(), "--skill"],
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == REPO_SKILL.read_bytes()
    assert not sai_home.exists()


def test_skill_is_eager_before_subcommands_and_repl():
    result = CliRunner().invoke(main, ["--skill", "market", "prices"])
    assert result.exit_code == 0
    assert result.stdout_bytes == REPO_SKILL.read_bytes()


def test_version_matches_package_metadata():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"sai, version {metadata.version('sai-cli')}\n"


def test_skill_fails_loudly_on_symlink_text(monkeypatch):
    # A checkout without symlink support stores the link target as text.
    monkeypatch.setattr(
        skill, "read_packaged_files", lambda: {"SKILL.md": b"../../skills/sai"}
    )
    for args in (["--skill"], ["skill", "show"], ["skill", "install"]):
        result = CliRunner().invoke(main, args)
        assert result.exit_code != 0, args
        assert "no frontmatter" in result.output
        assert "../../skills" not in result.stdout
