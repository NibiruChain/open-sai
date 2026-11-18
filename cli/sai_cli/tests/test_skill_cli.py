"""`sai skill show|source|install` and the packaged skill directory."""

from __future__ import annotations

import json
import sys
import tarfile
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
from click.testing import CliRunner
from sai_cli import skill
from sai_cli.sai_cli import main

REPO = Path(__file__).resolve().parents[3]
REPO_SKILL = REPO / "skills" / "sai"
sys.path.insert(0, str(REPO / "scripts"))

import check_dist

PAGES = ("cli", "mcp", "trading-safety", "references/contracts")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway HOME, so no test touches the real ~/.agents."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SAI_HOME", str(tmp_path / "sai-home"))
    assert Path.home() == home
    return home


def invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def repo_files() -> dict[str, bytes]:
    return check_dist.source_files(REPO_SKILL)


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_packaged_directory_is_the_repository_skill():
    files = skill.packaged_files()
    assert files == repo_files()
    for page in PAGES:
        assert f"{page}.md" in files
    assert files["SKILL.md"].startswith(b"---\nname: sai\n")


def test_skill_pages_link_only_to_files_that_ship():
    import re

    files = repo_files()
    for name, data in files.items():
        if not name.endswith(".md"):
            continue
        base = Path(name).parent
        for target in re.findall(r"\]\(([^)#\s]+)", data.decode()):
            if "://" in target:
                continue
            assert (base / target).as_posix() in files, f"{name} links to {target}"


def test_show_prints_skill_md_and_flag_is_an_alias(home):
    shown = invoke("skill", "show")
    assert shown.exit_code == 0
    assert shown.stdout_bytes == (REPO_SKILL / "SKILL.md").read_bytes()
    assert invoke("--skill").stdout_bytes == shown.stdout_bytes
    assert invoke("--json", "skill", "show").stdout_bytes == shown.stdout_bytes
    assert not (home / ".agents").exists()


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("cli", "cli.md"),
        ("mcp.md", "mcp.md"),
        ("trading-safety", "trading-safety.md"),
        ("references/contracts", "references/contracts.md"),
        ("vaults", "references/vaults.md"),
        ("SKILL", "SKILL.md"),
    ],
)
def test_show_page(name, path):
    result = invoke("skill", "show", "--page", name)
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == (REPO_SKILL / path).read_bytes()


def test_show_unknown_page_lists_pages():
    result = invoke("skill", "show", "--page", "nope")
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "trading-safety" in result.output


def test_show_ambiguous_page_name():
    files = {"SKILL.md": b"---\n", "a/x.md": b"a", "b/x.md": b"b"}
    assert skill.find_page(files, "a/x") == b"a"
    with pytest.raises(Exception, match="ambiguous"):
        skill.find_page(files, "x")


def test_source_json_names_both_repositories_and_the_synced_commit(home):
    result = invoke("--json", "skill", "source")
    assert result.exit_code == 0, result.output
    info = json.loads(result.stdout)
    record = json.loads((REPO_SKILL / ".source.json").read_text())
    assert info["name"] == "sai"
    assert info["cliVersion"] == metadata.version("sai-cli")
    assert Path(info["path"]).resolve() == REPO_SKILL.resolve()
    assert info["repositories"] == {
        "cli": "https://github.com/SaiDotFun/open-sai",
        "skill": "https://github.com/Unique-Divine/jiyuu",
    }
    assert info["source"]["commit"] == record["commit"]
    assert len(info["source"]["commit"]) == 40
    assert info["source"]["path"] == "ai-skills/sai"
    assert set(PAGES) <= set(info["pages"])
    assert info["install"] == {
        "sai": "sai skill install",
        "npx": "npx skills add SaiDotFun/open-sai --skill sai",
        "defaultDir": str(home / ".agents" / "skills" / "sai"),
    }


def test_source_text(home):
    result = invoke("skill", "source")
    assert result.exit_code == 0
    record = json.loads((REPO_SKILL / ".source.json").read_text())
    for expected in (
        "https://github.com/SaiDotFun/open-sai",
        "https://github.com/Unique-Divine/jiyuu",
        record["commit"],
        "sai skill install",
        "npx skills add SaiDotFun/open-sai --skill sai",
    ):
        assert expected in result.output


def test_source_without_a_record_says_so():
    info = skill.source_info({"SKILL.md": b"---\n"}, "1.2.3")
    assert info["source"]["commit"] is None
    assert "not recorded" in skill.render_source(info)
    assert skill.source_record({".source.json": b"not json"}) == {}


def test_install_defaults_to_agents_skills_under_home(home):
    result = invoke("--json", "skill", "install")
    assert result.exit_code == 0, result.output
    target = home / ".agents" / "skills" / "sai"
    report = json.loads(result.stdout)
    assert report["action"] == "installed"
    assert report["dir"] == str(target)
    assert report["files"] == len(repo_files())
    assert len(report["commit"]) == 40
    assert tree(target) == repo_files()

    again = invoke("skill", "install")
    assert again.exit_code == 0
    assert "already up to date" in again.output
    assert tree(target) == repo_files()


def test_install_into_dir_writes_the_skill_there(home, tmp_path):
    target = tmp_path / "claude" / "skills" / "sai"
    result = invoke("skill", "install", "--dir", str(target))
    assert result.exit_code == 0, result.output
    assert f"Installed the sai skill at {target}" in result.output
    assert tree(target) == repo_files()
    assert not (home / ".agents").exists()
    # Nothing is left beside the target.
    assert [p.name for p in target.parent.iterdir()] == ["sai"]


def test_install_refuses_differing_content_without_force(home, tmp_path):
    target = tmp_path / "sai"
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    (target / "cli.md").write_text("my edit\n")
    (target / "mine.md").write_text("extra\n")

    refused = invoke("skill", "install", "--dir", str(target))
    assert refused.exit_code == 1
    assert "differs from the packaged skill in 2 file(s)" in refused.output
    assert "cli.md" in refused.output and "mine.md" in refused.output
    assert (target / "cli.md").read_text() == "my edit\n"

    forced = invoke("--json", "skill", "install", "--dir", str(target), "--force")
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["action"] == "replaced"
    assert tree(target) == repo_files()


def test_install_refuses_a_symlink_without_force(home, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "SKILL.md").write_text("---\nname: sai\n---\nmanaged copy\n")
    target = home / ".agents" / "skills" / "sai"
    target.parent.mkdir(parents=True)
    target.symlink_to(managed)

    refused = invoke("skill", "install")
    assert refused.exit_code == 1
    assert "is a symlink" in refused.output
    assert target.is_symlink()

    forced = invoke("skill", "install", "--force")
    assert forced.exit_code == 0, forced.output
    assert "Replaced the sai skill" in forced.output
    assert not target.is_symlink()
    assert tree(target) == repo_files()
    # The link's old target keeps its content.
    assert tree(managed) == {"SKILL.md": b"---\nname: sai\n---\nmanaged copy\n"}


def test_install_never_replaces_a_directory_that_is_not_the_skill(home, tmp_path):
    target = tmp_path / "projects"
    target.mkdir()
    (target / "notes.txt").write_text("keep me\n")
    for extra in ([], ["--force"]):
        result = invoke("skill", "install", "--dir", str(target), *extra)
        assert result.exit_code == 1
        assert "something other than the sai skill" in result.output
    assert tree(target) == {"notes.txt": b"keep me\n"}

    as_file = tmp_path / "file"
    as_file.write_text("x")
    result = invoke("skill", "install", "--dir", str(as_file), "--force")
    assert result.exit_code != 0
    assert as_file.read_text() == "x"


def test_install_fills_an_empty_directory(home, tmp_path):
    target = tmp_path / "empty"
    target.mkdir()
    assert invoke("skill", "install", "--dir", str(target)).exit_code == 0
    assert tree(target) == repo_files()


# scripts/check_dist.py


def write_wheel(path: Path, files: dict[str, bytes], link: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(f"sai_cli/skills/{name}")
            info.external_attr = (0o120777 if name == link else 0o100644) << 16
            archive.writestr(info, data)
    return path


def write_sdist(path: Path, files: dict[str, bytes], link_dir: bool = False) -> Path:
    import io

    with tarfile.open(path, "w:gz") as archive:
        if link_dir:
            member = tarfile.TarInfo("sai_cli-0.1.0/cli/sai_cli/skills")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../skills/sai"
            archive.addfile(member)
        for name, data in files.items():
            member = tarfile.TarInfo(f"sai_cli-0.1.0/cli/sai_cli/skills/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def test_check_dist_accepts_archives_with_every_skill_file(tmp_path):
    files = {"SKILL.md": b"---\n", "cli.md": b"c", "references/rest.md": b"r"}
    write_wheel(tmp_path / "a.whl", files)
    write_sdist(tmp_path / "a.tar.gz", files)
    assert check_dist.check(tmp_path, files) == []


def test_check_dist_reports_missing_changed_and_extra_files(tmp_path):
    files = {"SKILL.md": b"---\n", "cli.md": b"c", "references/rest.md": b"r"}
    write_wheel(tmp_path / "a.whl", {"SKILL.md": b"---\n", "cli.md": b"other"})
    write_sdist(tmp_path / "a.tar.gz", {**files, "stray.md": b"s"})
    errors = check_dist.check(tmp_path, files)
    assert len(errors) == 3
    assert any("references/rest.md is missing" in e for e in errors)
    assert any("cli.md differs" in e for e in errors)
    assert any("stray.md is not in skills/sai" in e for e in errors)


def test_check_dist_rejects_symlinks_in_either_archive(tmp_path):
    files = {"SKILL.md": b"---\n"}
    write_wheel(tmp_path / "a.whl", files, link="SKILL.md")
    write_sdist(tmp_path / "a.tar.gz", {}, link_dir=True)
    errors = check_dist.check(tmp_path, files)
    assert len(errors) == 2
    assert any("is a symlink" in e for e in errors)
    assert any("is not a regular file" in e for e in errors)
