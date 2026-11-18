"""scripts/install.py against a loopback release stub and a fake `uv`."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sai_cli.core import update
from test_update import API_PATH, WEB_PATH, Routes, Stub, listing, redirect

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
SCRIPT = SCRIPTS / "install.py"
sys.path.insert(0, str(SCRIPTS))

import install

GIT = "git+https://github.com/SaiDotFun/open-sai"

FAKE_UV = """#!{python}
import json, os, sys
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print("fake uv ran")
sys.exit(int(os.environ.get("FAKE_UV_EXIT", "0")))
"""


@pytest.fixture
def fake_uv(tmp_path) -> dict[str, str]:
    """An environment whose only `uv` records its arguments and exits."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV.format(python=sys.executable))
    uv.chmod(0o755)
    return {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path / "home"),
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
    }


def run(env: dict[str, str], stub: Stub, *args: str) -> subprocess.CompletedProcess:
    """Run the script as a person would, with nothing of this process's
    environment, so a real uv is never reached."""
    command = [sys.executable, str(SCRIPT), "--api-base", stub.url]
    command += ["--web-base", stub.url, *args]
    return subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=180, check=False
    )


def calls(env: dict[str, str]) -> list[list[str]]:
    log = Path(env["FAKE_UV_LOG"])
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_the_script_agrees_with_the_cli_about_releases():
    for name in ("REPO", "API_BASE", "WEB_BASE", "TAG_PREFIX", "PAGE_SIZE"):
        assert getattr(install, name) == getattr(update, name), name
    assert (install.MAX_PAGES, install.MAX_BODY) == (update.MAX_PAGES, update.MAX_BODY)
    tags = ("sai-cli/v0.1.0", "sai-cli/v0.2.0-rc.1", "v0.1.0", "sai-rs/v0.1.0", "")
    for tag in tags:
        assert install.parse_tag(tag) == update.parse_tag(tag), tag
    assert install.wheel_url("sai-cli/v1.2.3") == (
        update.release_for("sai-cli/v1.2.3").wheel_url
    )
    assert update.INSTALL_COMMAND.endswith("/main/scripts/install.py")


def test_installs_the_wheel_of_the_highest_sai_cli_release(fake_uv):
    tags = ("sai-rs/v9.0.0", "sai-cli/v0.3.0-rc.1", "sai-cli/v0.2.0", "v8.0.0")
    with Stub({API_PATH: listing(*tags, "sai-cli/v0.10.0")}) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    wheel = (
        f"{stub.url}/{update.REPO}/releases/download/sai-cli/v0.10.0"
        "/sai_cli-0.10.0-py3-none-any.whl"
    )
    assert calls(fake_uv) == [["tool", "install", wheel]]
    lines = result.stdout.splitlines()
    assert lines[0] == "Installing sai-cli 0.10.0 from its release wheel."
    assert lines[1] == f"$ uv tool install {wheel}"
    assert lines[-1] == "Installed. Run `sai --help` to start."
    assert result.stderr == ""


def test_source_builds_the_release_tag_from_git(fake_uv):
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        result = run(fake_uv, stub, "--source")
    assert result.returncode == 0, result.stderr
    assert calls(fake_uv) == [["tool", "install", f"{GIT}@sai-cli/v0.2.0"]]
    assert "Building sai-cli 0.2.0 from source." in result.stdout


@pytest.mark.parametrize("flags", [(), ("--source",)])
@pytest.mark.parametrize("tags", [(), ("sai-rs/v1.0.0", "sai-cli/v0.1.0-rc.1")])
def test_falls_back_to_git_main_with_a_note_before_the_first_release(
    fake_uv, flags, tags
):
    with Stub({API_PATH: listing(*tags)}) as stub:
        result = run(fake_uv, stub, *flags)
    assert result.returncode == 0, result.stderr
    assert calls(fake_uv) == [["tool", "install", GIT]]
    assert result.stdout.splitlines()[0] == (
        "No sai-cli release is published yet; installing from source (main)."
    )


def test_uses_the_web_redirect_when_the_api_is_rate_limited(fake_uv):
    location = f"https://github.com/{update.REPO}/releases/tag/sai-cli%2Fv0.3.1"
    routes: Routes = {API_PATH: (403, {}, b"{}"), WEB_PATH: redirect(location)}
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    assert stub.requests == [("GET", API_PATH), ("HEAD", WEB_PATH)]
    assert calls(fake_uv)[0][2].endswith(
        "/sai-cli/v0.3.1/sai_cli-0.3.1-py3-none-any.whl"
    )


def test_rate_limited_with_no_release_still_falls_back_to_git(fake_uv):
    routes: Routes = {
        API_PATH: (429, {}, b""),
        WEB_PATH: redirect(f"https://github.com/{update.REPO}/releases"),
    }
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 0, result.stderr
    assert calls(fake_uv) == [["tool", "install", GIT]]


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({API_PATH: (200, {}, b"not json")}, "malformed JSON"),
        ({}, "HTTP 404"),
        (
            {
                API_PATH: (500, {}, b""),
                WEB_PATH: redirect("/SaiDotFun/open-sai/releases/tag/sai-rs/v1.0.0"),
            },
            "is not a sai-cli release",
        ),
    ],
)
def test_a_failed_lookup_installs_nothing_and_names_the_source_install(
    fake_uv, routes, message
):
    with Stub(routes) as stub:
        result = run(fake_uv, stub)
    assert result.returncode == 1
    assert message in result.stderr
    assert f"uv tool install {GIT}" in result.stderr
    assert calls(fake_uv) == []


def test_dry_run_prints_the_command_and_runs_nothing(fake_uv):
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        result = run(fake_uv, stub, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "$ uv tool install http" in result.stdout
    assert "Dry run: nothing was installed." in result.stdout
    assert calls(fake_uv) == []


def test_reports_a_failed_uv_and_a_missing_uv(fake_uv, tmp_path):
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        failed = run({**fake_uv, "FAKE_UV_EXIT": "2"}, stub)
        empty = tmp_path / "empty"
        empty.mkdir()
        missing = run({**fake_uv, "PATH": str(empty)}, stub)
    assert failed.returncode == 2
    assert "uv tool install failed with exit code 2" in failed.stderr
    assert "Installed." not in failed.stdout
    assert missing.returncode == 1
    assert "uv is not on PATH" in missing.stderr


def test_prefers_the_uv_that_runs_the_script(fake_uv, tmp_path):
    """`uv run` sets UV to its own path; the script installs with that uv."""
    runner = tmp_path / "runner-uv"
    runner.write_text(FAKE_UV.format(python=sys.executable))
    runner.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**fake_uv, "PATH": str(empty), "UV": str(runner)}
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        result = run(env, stub)
    assert result.returncode == 0, result.stderr
    assert len(calls(env)) == 1
