"""`sai update` installs: real `uv` against a loopback release built from a
throwaway package, plus the lock, the notices, and the daily background run.

Every uv tool directory, the uv cache, and SAI_HOME are temporary, so the
real installed sai and ~/.sai are never touched.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from sai_cli.core import update, updater
from sai_cli.sai_cli import EXIT_UNSUPPORTED, auto_update_applies, main
from test_update import API_PATH, Routes, Stub, listing, make_prefix

REAL_UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(REAL_UV is None, reason="uv is not on PATH")
real_uv = [pytest.mark.slow, needs_uv]


# A throwaway sai-cli release


def build_wheel(version: str, server_body: str = "") -> bytes:
    """Build a dependency-free sai-cli wheel whose `sai --version` prints
    VERSION. SERVER_BODY is the source of sai_cli.mcp_server.server."""
    info = f"sai_cli-{version}.dist-info"
    files = {
        "sai_cli/__init__.py": "",
        "sai_cli/sai_cli.py": (f'def main():\n    print("sai, version {version}")\n'),
        "sai_cli/mcp_server/__init__.py": "",
        "sai_cli/mcp_server/server.py": server_body,
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: sai-cli\nVersion: {version}\n"
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{info}/entry_points.txt": "[console_scripts]\nsai = sai_cli.sai_cli:main\n",
    }
    record = []
    for path, text in files.items():
        digest = hashlib.sha256(text.encode()).digest()
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        record.append(f"{path},sha256={encoded},{len(text.encode())}")
    record.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = "\n".join(record) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return buffer.getvalue()


def downloads(version: str) -> str:
    return f"/{update.REPO}/releases/download/sai-cli/v{version}"


def release_routes(
    wheels: dict[str, bytes],
    latest: Sequence[str] | None = None,
    bad_hash: Sequence[str] = (),
) -> Routes:
    """Routes serving WHEELS (version -> wheel bytes) as GitHub releases,
    with the release list naming LATEST (default: every version)."""
    tags = [update.tag_for(version) for version in (latest or wheels)]
    routes: Routes = {API_PATH: listing("sai-rs/v9.9.9", *tags)}
    for version, data in wheels.items():
        name = update.wheel_name(version)
        digest = hashlib.sha256(data).hexdigest()
        if version in bad_hash:
            digest = hashlib.sha256(b"something else").hexdigest()
        sums = f"{'0' * 64}  sai_cli-{version}.tar.gz\n{digest}  {name}\n"
        routes[f"{downloads(version)}/{name}"] = (200, {}, data)
        routes[f"{downloads(version)}/SHA256SUMS"] = (200, {}, sums.encode())
    return routes


class Tool:
    """A temporary uv tool home holding one `sai-cli` install."""

    def __init__(self, root: Path, base: str) -> None:
        self.base = base
        self.prefix = root / "tools" / "sai-cli"
        self.sai = root / "bin" / "sai"
        self.home = root / "sai-home"

    def uv_install(self, spec: str) -> None:
        assert REAL_UV is not None
        subprocess.run(
            [REAL_UV, "tool", "install", spec],
            check=True,
            capture_output=True,
            timeout=120,
        )

    def install_release(self, version: str) -> None:
        name = update.wheel_name(version)
        self.uv_install(f"{self.base}{downloads(version)}/{name}")

    def version(self) -> str:
        done = subprocess.run(
            [str(self.sai), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip().removeprefix("sai, version ")

    def options(self, **changes) -> updater.Options:
        options = updater.Options(
            api_base=self.base, web_base=self.base, prefix=self.prefix
        )
        return replace(options, **changes)

    def update(self, **changes) -> updater.Outcome:
        return updater.update_now(self.options(**changes))

    def kind(self) -> str:
        return update.install_kind(self.prefix, self.base).kind


@pytest.fixture
def sai_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "sai-home"
    monkeypatch.setenv("SAI_HOME", str(home))
    return home


@pytest.fixture
def uv_dirs(tmp_path, monkeypatch, sai_home) -> Path:
    """Point uv at temporary tool, bin, and cache directories."""
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    monkeypatch.delenv("UV", raising=False)
    return tmp_path


def user_files(home: Path) -> dict[str, bytes]:
    """SAI_HOME files that belong to the user, not to the updater."""
    own = {"update.json", "update.lock", "update.stamp"}
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file() and path.relative_to(home).parts[0] not in own | {"cache"}
    }


BROKEN_IMPORT = 'raise ImportError("this release cannot import")\n'


# Real uv


@pytest.mark.slow
@needs_uv
def test_upgrade_keeps_the_sai_path_and_the_users_files(uv_dirs, sai_home):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    (sai_home / "trader").mkdir(parents=True)
    (sai_home / "trader" / "sessions.json").write_bytes(b'{"keep": "me"}\n')
    (sai_home / "config.toml").write_bytes(b"[update]\nauto = true\n")
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        assert tool.version() == "0.1.0"
        assert tool.kind() == update.UV_TOOL_RELEASE
        link = os.readlink(tool.sai) if tool.sai.is_symlink() else None
        before = user_files(sai_home)

        outcome = tool.update()

        assert outcome.status == updater.UPDATED, outcome.message
        assert (outcome.installed, outcome.target) == ("0.2.0", "0.2.0")
        assert outcome.exit_code == 0
        assert tool.version() == "0.2.0"
        assert (os.readlink(tool.sai) if tool.sai.is_symlink() else None) == link
        assert user_files(sai_home) == before
        # The install now comes from the verified wheel in the cache, and
        # still counts as a release install for the next update.
        assert (update.wheel_cache() / update.wheel_name("0.2.0")).is_file()
        assert tool.kind() == update.UV_TOOL_RELEASE
        state = updater.read_state()
        assert state["latest"] == "sai-cli/v0.2.0"
        assert state["result"]["status"] == updater.UPDATED
        assert (state["result"]["from"], state["result"]["to"]) == ("0.1.0", "0.2.0")
        # The person watched this update, so no later notice repeats it.
        assert updater.notice(state, "0.2.0") is None

        again = tool.update()
        assert again.status == updater.CURRENT
        assert tool.version() == "0.2.0"


@pytest.mark.slow
@needs_uv
def test_bad_hash_keeps_the_old_version(uv_dirs):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    with Stub(release_routes(wheels, bad_hash=["0.2.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert outcome.exit_code == 1
    assert "does not match its SHA256SUMS entry" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    assert not outcome.rolled_back
    assert tool.version() == "0.1.0"
    assert list(update.wheel_cache().iterdir()) == []
    assert updater.read_state()["result"]["status"] == updater.FAILED


@pytest.mark.slow
@needs_uv
def test_broken_wheel_keeps_the_old_version(uv_dirs):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": b"this is not a wheel"}
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        outcome = tool.update()
    assert outcome.status == updater.FAILED
    assert "uv tool install failed" in outcome.message
    assert tool.version() == "0.1.0"
    assert list(update.wheel_cache().glob("*.whl")) == []


@pytest.mark.slow
@needs_uv
def test_a_release_that_fails_to_import_rolls_back(uv_dirs):
    wheels = {
        "0.1.0": build_wheel("0.1.0"),
        "0.2.0": build_wheel("0.2.0"),
        "0.3.0": build_wheel("0.3.0", BROKEN_IMPORT),
    }
    with Stub(release_routes(wheels, latest=["0.3.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")

        # From a release URL install: rolls back by installing that URL again.
        first = tool.update()
        assert first.status == updater.FAILED
        assert first.rolled_back
        assert "the package does not import" in first.message
        assert "Rolled back to sai 0.1.0" in first.message
        assert tool.version() == "0.1.0"
        assert tool.kind() == update.UV_TOOL_RELEASE

        # From an install sai update made: rolls back from the kept wheel.
        assert tool.update(to="0.2.0").status == updater.UPDATED
        stub.routes.pop(f"{downloads('0.2.0')}/{update.wheel_name('0.2.0')}")
        second = tool.update()
        assert second.status == updater.FAILED
        assert second.rolled_back
        assert tool.version() == "0.2.0"
        kept = sorted(path.name for path in update.wheel_cache().glob("*.whl"))
        assert kept == [update.wheel_name("0.2.0")]
    result = updater.read_state()["result"]
    assert (result["status"], result["to"], result["rolledBack"]) == (
        updater.FAILED,
        "0.3.0",
        True,
    )


@pytest.mark.slow
@needs_uv
def test_never_downgrades_unless_told_to(uv_dirs):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    with Stub(release_routes(wheels, latest=["0.1.0"])) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.2.0")
        kept = tool.update()
        assert kept.status == updater.CURRENT
        assert "newer than the latest release 0.1.0" in kept.message
        assert tool.version() == "0.2.0"

        missing = tool.update(to="0.9.0")
        assert missing.status == updater.FAILED
        assert "the release has no file" in missing.message
        assert tool.version() == "0.2.0"

        back = tool.update(to="0.1.0")
        assert back.status == updater.UPDATED, back.message
        assert tool.version() == "0.1.0"


WORKER = """
import json, sys
from pathlib import Path
from sai_cli.core import updater
base, prefix = sys.argv[1:3]
options = updater.Options(api_base=base, web_base=base, prefix=Path(prefix))
print(json.dumps(updater.update_now(options).to_json()))
"""

UV_SHIM = """#!{python}
import os, sys
with open({log!r}, "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
os.execv({uv!r}, [{uv!r}, *sys.argv[1:]])
"""


@pytest.mark.slow
@needs_uv
def test_two_workers_install_once(uv_dirs, monkeypatch):
    wheels = {"0.1.0": build_wheel("0.1.0"), "0.2.0": build_wheel("0.2.0")}
    log = uv_dirs / "uv.log"
    shims = uv_dirs / "shims"
    shims.mkdir()
    shim = shims / "uv"
    shim.write_text(UV_SHIM.format(python=sys.executable, log=str(log), uv=REAL_UV))
    shim.chmod(0o755)
    with Stub(release_routes(wheels)) as stub:
        tool = Tool(uv_dirs, stub.url)
        tool.install_release("0.1.0")
        monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
        command = [sys.executable, "-c", WORKER, stub.url, str(tool.prefix)]
        workers = [
            subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        outcomes = [
            json.loads(worker.communicate(timeout=180)[0]) for worker in workers
        ]
    assert sorted(outcome["status"] for outcome in outcomes) == [
        updater.CURRENT,
        updater.UPDATED,
    ]
    installs = [line for line in log.read_text().splitlines() if "tool install" in line]
    assert len(installs) == 1
    assert tool.version() == "0.2.0"


# Scripted runner: the commands an update runs, without uv


class Script:
    """A runner that records commands and answers `sai --version` with the
    version the last `uv tool install` named."""

    def __init__(self, version: str, fail_install: bool = False) -> None:
        self.version = version
        self.fail_install = fail_install
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], timeout: float) -> tuple[int, str]:
        self.commands.append(list(command))
        if command[1:3] == ["tool", "install"]:
            if self.fail_install:
                return 2, "error: no such revision"
            self.version = command[-1].rsplit("v", 1)[-1]
            return 0, "Installed 1 executable: sai"
        if command[-1] == "--version":
            return 0, f"sai, version {self.version}\n"
        return 0, ""


GIT = {
    "url": "https://github.com/SaiDotFun/open-sai",
    "vcs_info": {
        "vcs": "git",
        "commit_id": "a" * 40,
        "requested_revision": "sai-cli/v0.1.0",
    },
}


def scripted(tmp_path: Path, stub: Stub, script: Script, **changes) -> updater.Options:
    prefix = make_prefix(tmp_path / "tools" / "sai-cli", GIT, receipt=True)
    return updater.Options(
        api_base=stub.url,
        web_base=stub.url,
        prefix=prefix,
        runner=script,
        uv="/fake/uv",
        **changes,
    )


def test_source_builds_the_release_tag_from_git(tmp_path, sai_home):
    script = Script("0.1.0")
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
    assert outcome.status == updater.UPDATED, outcome.message
    spec = "git+https://github.com/SaiDotFun/open-sai@sai-cli/v0.2.0"
    assert script.commands[0] == ["/fake/uv", "tool", "install", "--force", spec]
    assert [command[-1] for command in script.commands[1:]] == [
        "--version",
        "import sai_cli.sai_cli, sai_cli.mcp_server.server",
    ]
    assert stub.requests == [("GET", API_PATH)]
    assert not update.wheel_cache().exists()


def test_a_missing_source_tag_keeps_the_old_version(tmp_path, sai_home):
    script = Script("0.1.0", fail_install=True)
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script, source=True))
    assert outcome.status == updater.FAILED
    assert "no such revision" in outcome.message
    assert "sai 0.1.0 is unchanged" in outcome.message
    # One install attempt, then a check that the old version still runs.
    installs = [c for c in script.commands if c[1:3] == ["tool", "install"]]
    assert len(installs) == 1


def test_no_release_and_bad_versions_install_nothing(tmp_path, sai_home):
    script = Script("0.1.0")
    with Stub({API_PATH: listing("sai-rs/v1.0.0")}) as stub:
        none = updater.update_now(scripted(tmp_path, stub, script))
        bad = updater.update_now(scripted(tmp_path / "b", stub, script, to="latest"))
    assert none.status == updater.NO_RELEASE
    assert none.exit_code == 0
    assert bad.status == updater.FAILED
    assert "is not a version like 0.2.0" in bad.message
    assert script.commands == []


def test_a_failed_check_is_reported_and_installs_nothing(tmp_path, sai_home):
    script = Script("0.1.0")
    with Stub({API_PATH: (200, {}, b"garbage")}) as stub:
        outcome = updater.update_now(scripted(tmp_path, stub, script))
    assert outcome.status == updater.FAILED
    assert "update check failed" in outcome.message
    assert script.commands == []
    # A failed lookup is not an update attempt; doctor has nothing to warn of.
    assert "result" not in updater.read_state()


@pytest.mark.parametrize(
    ("name", "direct", "receipt", "text"),
    [
        (
            "checkout",
            {"url": "file:///src", "dir_info": {"editable": True}},
            False,
            "uv sync",
        ),
        (
            "fork",
            {**GIT, "url": "https://github.com/someone/open-sai"},
            True,
            "install.py",
        ),
        ("pip", None, False, "install.py"),
    ],
)
def test_unofficial_installs_are_not_updated(
    tmp_path, sai_home, name, direct, receipt, text
):
    prefix = make_prefix(tmp_path / name, direct, receipt=receipt)
    script = Script("0.1.0")
    outcome = updater.update_now(updater.Options(prefix=prefix, runner=script))
    assert outcome.status == updater.UNSUPPORTED
    assert outcome.exit_code == EXIT_UNSUPPORTED == 3
    assert text in outcome.message
    assert script.commands == []
    assert not sai_home.exists()


def test_sai_update_from_a_checkout_exits_3_with_a_note(sai_home):
    result = CliRunner().invoke(main, ["update"])
    assert result.exit_code == EXIT_UNSUPPORTED
    assert result.stdout == ""
    assert "only updates an official uv tool install" in result.stderr
    data = CliRunner().invoke(main, ["--json", "update"])
    assert data.exit_code == EXIT_UNSUPPORTED
    assert json.loads(data.stdout)["status"] == updater.UNSUPPORTED


def test_check_takes_no_install_options(sai_home):
    result = CliRunner().invoke(main, ["update", "--check", "--to", "0.2.0"])
    assert result.exit_code == 2
    assert "--check takes no other update option" in result.stderr


# Background run


def test_notify_only_records_that_an_update_is_available(tmp_path, sai_home):
    script = Script("0.1.0")
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        options = scripted(tmp_path, stub, script, install=False, background=True)
        outcome = updater.update_now(options)
    assert outcome.status == updater.AVAILABLE
    assert script.commands == []
    state = updater.read_state()
    result = state["result"]
    assert (result["status"], result["to"]) == (updater.AVAILABLE, "0.2.0")
    assert updater.notice(state, "0.1.0") is not None


def test_background_run_skips_when_another_update_holds_the_lock(tmp_path, sai_home):
    script = Script("0.1.0")
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub, updater.locked() as held:
        assert held
        options = scripted(tmp_path, stub, script, background=True)
        outcome = updater.update_now(options)
    assert outcome.status == updater.BUSY
    assert outcome.exit_code == 0
    assert stub.requests == []
    assert script.commands == []


def test_background_run_does_not_retry_a_version_that_failed(tmp_path, sai_home):
    script = Script("0.1.0", fail_install=True)
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}) as stub:
        options = scripted(tmp_path, stub, script, source=True, background=True)
        first = updater.update_now(options)
        attempts = len(script.commands)
        second = updater.update_now(options)
        manual = updater.update_now(replace(options, background=False))
    assert first.status == second.status == updater.FAILED
    assert len(script.commands) > attempts
    assert "already failed" in second.message
    assert "no such revision" in manual.message


def test_lock_waits_for_the_holder(sai_home):
    released = threading.Event()

    def hold() -> None:
        with updater.locked() as held:
            assert held
            released.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        time.sleep(0.1)
        with updater.locked() as busy:
            assert not busy
        threading.Timer(0.2, released.set).start()
        with updater.locked(wait=5) as held:
            assert held
    finally:
        released.set()
        holder.join()


def test_expected_hash_reads_sha256sums():
    digest = "ab" * 32
    sums = (
        f"{'cd' * 32}  sai_cli-0.2.0.tar.gz\n{digest} *sai_cli-0.2.0-py3-none-any.whl\n"
    )
    assert updater.expected_hash(sums, "sai_cli-0.2.0-py3-none-any.whl") == digest
    assert updater.expected_hash(sums, "other.whl") is None
    assert updater.expected_hash("short  other.whl\n", "other.whl") is None


# Notices and the daily tick


def state_with(status: str, to: str = "0.2.0", **extra) -> dict:
    return {"schema": 1, "result": {"status": status, "to": to}, **extra}


def test_notice_names_each_state_once():
    key, text = updater.notice(state_with(updater.UPDATED), "0.2.0") or ("", "")
    assert key == "updated:0.2.0"
    assert "updated itself to 0.2.0" in text
    assert updater.notice(state_with(updater.UPDATED, notified=key), "0.2.0") is None

    available = updater.notice(state_with(updater.AVAILABLE), "0.1.0")
    assert available is not None and "sai 0.2.0 is available" in available[1]
    failed = updater.notice(state_with(updater.FAILED), "0.1.0")
    assert failed is not None and "could not update itself to 0.2.0" in failed[1]
    # A newer result is a new state even after an older one was announced.
    assert updater.notice(state_with(updater.UPDATED, "0.3.0", notified=key), "0.3.0")


def test_notice_is_silent_when_there_is_nothing_to_say():
    assert updater.notice({}, "0.1.0") is None
    assert updater.notice({"result": "junk"}, "0.1.0") is None
    assert updater.notice(state_with(updater.CURRENT), "0.2.0") is None
    assert updater.notice(state_with(updater.UPDATED, to="junk"), "0.2.0") is None
    # Already at or past the version: the offer and the failure are stale.
    assert updater.notice(state_with(updater.AVAILABLE), "0.2.0") is None
    assert updater.notice(state_with(updater.FAILED), "0.3.0") is None


def official_prefix(tmp_path: Path) -> Path:
    return make_prefix(tmp_path / "tools" / "sai-cli", GIT, receipt=True)


def test_tick_prints_a_notice_once_and_starts_one_background_run(tmp_path, sai_home):
    prefix = official_prefix(tmp_path)
    updater.write_state({"result": {"status": updater.AVAILABLE, "to": "0.2.0"}})
    printed: list[str] = []
    started: list[list[str]] = []

    def start(command: Sequence[str]) -> None:
        started.append(list(command))

    for _ in range(3):
        updater.tick(printed.append, start, prefix)
    assert printed == ["sai 0.2.0 is available. Run `sai update` to install it."]
    assert started == [[sys.executable, "-m", "sai_cli", "update", "--background"]]
    assert updater.read_state()["notified"] == "available:0.2.0"

    # A day later the check is due again.
    old = time.time() - updater.CHECK_INTERVAL - 1
    os.utime(updater.stamp_path(), (old, old))
    updater.tick(printed.append, start, prefix)
    assert len(started) == 2
    assert len(printed) == 1


def test_tick_holds_the_notice_while_an_update_runs(tmp_path, sai_home):
    prefix = official_prefix(tmp_path)
    updater.write_state({"result": {"status": updater.UPDATED, "to": "0.1.0"}})
    printed: list[str] = []
    with updater.locked() as held:
        assert held
        updater.tick(printed.append, lambda command: None, prefix)
    assert printed == []
    updater.tick(printed.append, lambda command: None, prefix)
    assert len(printed) == 1


def test_tick_leaves_unofficial_installs_alone(tmp_path, sai_home):
    editable = {"url": "file:///src", "dir_info": {"editable": True}}
    prefix = make_prefix(tmp_path / "checkout" / ".venv", editable)
    started: list[list[str]] = []
    updater.tick(
        lambda text: None, lambda command: started.append(list(command)), prefix
    )
    assert started == []
    assert not sai_home.exists()


def test_tick_never_raises(tmp_path, sai_home):
    def explode(command: Sequence[str]) -> None:
        raise OSError("cannot start")

    updater.tick(lambda text: None, explode, official_prefix(tmp_path))


def test_spawn_starts_a_detached_process(tmp_path):
    marker = tmp_path / "ran"
    code = f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getsid(0)))"
    updater.spawn([sys.executable, "-c", code])
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.read_text() != str(os.getsid(0))


@pytest.mark.parametrize(
    ("subcommand", "json_output", "environ", "terminal", "expected"),
    [
        ("market", False, {}, True, True),
        (None, False, {}, True, True),
        ("market", True, {}, True, False),
        ("market", False, {"CI": "true"}, True, False),
        ("market", False, {}, False, False),
        ("mcp", False, {}, True, False),
        ("update", False, {}, True, False),
        ("doctor", False, {}, True, False),
    ],
)
def test_auto_update_applies_only_to_interactive_commands(
    subcommand, json_output, environ, terminal, expected
):
    assert auto_update_applies(subcommand, json_output, environ, terminal) is expected


def test_commands_under_the_test_runner_never_tick(sai_home, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    result = CliRunner().invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert not updater.stamp_path().exists()
