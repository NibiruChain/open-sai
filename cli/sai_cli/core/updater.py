"""Install a sai-cli release over this install, verify it, and roll back.

`sai update` downloads the release wheel and its SHA256SUMS, checks the hash,
installs the wheel with `uv tool install --force`, and then runs the new
`sai --version` and imports the package. A failure at any step leaves or
restores the previous version. Only an official uv tool install is changed.

Interactive commands call `tick`, which prints at most one notice per state
change and starts a detached `sai update --background` at most once a day.

Everything this module uses from the package and its dependencies is imported
at the top: uv replaces those files while this process runs, so none of them
may be imported after the install starts.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import update

EXIT_OK = 0
EXIT_FAILED = 1
# The install is one `sai update` does not change.
EXIT_UNSUPPORTED = 3

MAX_WHEEL = 64 << 20
MAX_SUMS = 1 << 16
DOWNLOAD_TIMEOUT = 120.0
UV_TIMEOUT = 600.0
VERIFY_TIMEOUT = 60.0
# How long a foreground `sai update` waits for another update to finish.
LOCK_WAIT = 300.0
# The background check runs at most this often.
CHECK_INTERVAL = 24 * 60 * 60
# Modules the new version must import: the CLI and the MCP server.
IMPORTS = ("sai_cli.sai_cli", "sai_cli.mcp_server.server")

UPDATED = "updated"
CURRENT = "current"
AVAILABLE = "available"
FAILED = "failed"
UNSUPPORTED = "unsupported"
NO_RELEASE = "no-release"
BUSY = "busy"

# Runs a command and returns its exit code and its combined output.
Runner = Callable[[Sequence[str], float], tuple[int, str]]


class UpdateFailed(Exception):
    """A step of the update failed; the message says which."""


# State


def lock_path() -> Path:
    return update.sai_home() / "update.lock"


def stamp_path() -> Path:
    """An empty file whose modification time is when the last background
    check started."""
    return update.sai_home() / "update.stamp"


def read_state(path: Path | None = None) -> dict[str, Any]:
    """Return update.json as a dict, or {} when it is missing or unreadable.

    `latest` and `checkedAt` are the last check. `result` is the last update
    attempt and `notified` the notice already shown for it.
    """
    try:
        data = json.loads((path or update.cache_path()).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != update.CACHE_SCHEMA:
        return {}
    return data


def write_state(changes: dict[str, Any], path: Path | None = None) -> None:
    """Merge CHANGES into update.json atomically. Callers hold the lock."""
    target = path or update.cache_path()
    state = {**read_state(target), **changes, "schema": update.CACHE_SCHEMA}
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def locked(wait: float = 0.0) -> Generator[bool]:
    """Hold update.lock, so one process at a time installs or writes
    update.json. Yields False when another process still holds it after WAIT
    seconds."""
    path = lock_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        held = _try_lock(fd)
        while not held and time.monotonic() < deadline:
            time.sleep(0.1)
            held = _try_lock(fd)
        yield held
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


# Download and verify


def _download(
    client: httpx.Client, url: str, target: Path, limit: int, deadline: float
) -> None:
    name = url.rsplit("/", 1)[-1]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpdateFailed(f"timed out downloading {name}")
    timeout = httpx.Timeout(min(remaining, 30.0))
    size = 0
    with client.stream("GET", url, timeout=timeout) as response:
        if response.status_code == 404:
            raise UpdateFailed(f"the release has no file {name}")
        if response.status_code != 200:
            raise UpdateFailed(
                f"downloading {name} answered HTTP {response.status_code}"
            )
        with target.open("wb") as handle:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise UpdateFailed(f"{name} is larger than expected")
                if time.monotonic() > deadline:
                    raise UpdateFailed(f"timed out downloading {name}")
                handle.write(chunk)


def expected_hash(sums: str, name: str) -> str | None:
    """Return the SHA-256 that SHA256SUMS text lists for file NAME."""
    for line in sums.splitlines():
        digest, _, listed = line.strip().partition(" ")
        if listed.strip().removeprefix("*") == name and len(digest) == 64:
            return digest.lower()
    return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_wheel(release: update.Release, directory: Path) -> Path:
    """Download RELEASE's wheel and SHA256SUMS into DIRECTORY and return the
    wheel, or raise UpdateFailed when the hash does not match."""
    name = update.wheel_name(release.version)
    wheel, sums = directory / name, directory / "SHA256SUMS"
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    headers = {"User-Agent": "sai-cli"}
    try:
        with httpx.Client(headers=headers, follow_redirects=True) as client:
            _download(client, release.sums_url, sums, MAX_SUMS, deadline)
            _download(client, release.wheel_url, wheel, MAX_WHEEL, deadline)
    except httpx.HTTPError as exc:
        raise UpdateFailed(f"download failed: {type(exc).__name__}") from exc
    expected = expected_hash(sums.read_text(errors="replace"), name)
    if expected is None:
        raise UpdateFailed(f"SHA256SUMS does not list {name}")
    if sha256(wheel) != expected:
        raise UpdateFailed(f"{name} does not match its SHA256SUMS entry")
    return wheel


# Install, verify, roll back


def run(command: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run COMMAND and return its exit code and combined output."""
    try:
        done = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g} s"
    except OSError as exc:
        return 127, f"could not start: {type(exc).__name__}"
    return done.returncode, done.stdout


def find_uv() -> str | None:
    return shutil.which("uv")


def _last_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"


def _scripts(prefix: Path) -> tuple[Path, Path]:
    """The `sai` and `python` executables of the environment at PREFIX."""
    if os.name == "nt":
        return prefix / "Scripts" / "sai.exe", prefix / "Scripts" / "python.exe"
    return prefix / "bin" / "sai", prefix / "bin" / "python"


def verify(
    prefix: Path, version: str, imports: Sequence[str], runner: Runner
) -> str | None:
    """Return why the install at PREFIX is not a working VERSION, or None."""
    sai, python = _scripts(prefix)
    code, output = runner([str(sai), "--version"], VERIFY_TIMEOUT)
    if code != 0:
        return f"`sai --version` failed: {_last_line(output)}"
    if not output.strip().endswith(f" {version}"):
        return f"`sai --version` printed {_last_line(output)!r}, not {version}"
    statement = "import " + ", ".join(imports)
    code, output = runner([str(python), "-c", statement], VERIFY_TIMEOUT)
    if code != 0:
        return f"the package does not import: {_last_line(output)}"
    return None


def install_spec(uv: str, spec: str, runner: Runner) -> str | None:
    """Run `uv tool install --force SPEC`; return the error, or None."""
    code, output = runner([uv, "tool", "install", "--force", spec], UV_TIMEOUT)
    return None if code == 0 else f"uv tool install failed: {_last_line(output)}"


def previous_spec(install: update.Install, installed: str) -> str | None:
    """What to install to get the current version back, or None.

    The wheel kept from the last update comes first; otherwise the source
    this install records: the Git commit or the release wheel URL.
    """
    cached = update.wheel_cache() / update.wheel_name(installed)
    if cached.is_file():
        return str(cached)
    if install.kind == update.UV_TOOL_GIT and install.url and install.commit:
        return f"git+{install.url}@{install.commit}"
    if install.kind == update.UV_TOOL_RELEASE and install.url:
        return None if install.url.startswith("file:") else install.url
    return None


def prune_wheels(keep: Sequence[str]) -> None:
    """Delete cached wheels other than the versions in KEEP."""
    names = {update.wheel_name(version) for version in keep}
    try:
        for path in update.wheel_cache().glob("sai_cli-*.whl"):
            if path.name not in names:
                path.unlink(missing_ok=True)
    except OSError:
        pass


@dataclass(frozen=True)
class Outcome:
    """What an update did, for the terminal, `--json`, and update.json."""

    status: str
    message: str
    installed: str | None = None
    target: str | None = None
    rolled_back: bool = False

    @property
    def exit_code(self) -> int:
        if self.status == UNSUPPORTED:
            return EXIT_UNSUPPORTED
        return EXIT_FAILED if self.status == FAILED else EXIT_OK

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "installed": self.installed,
            "target": self.target,
            "rolledBack": self.rolled_back,
        }


@dataclass(frozen=True)
class Options:
    """One update run. The defaults are what `sai update` does."""

    api_base: str = update.API_BASE
    web_base: str = update.WEB_BASE
    git_url: str = update.GIT_URL
    # Install this version instead of the latest; the only way to go back.
    to: str | None = None
    # Build the release from Git instead of installing its wheel.
    source: bool = False
    # False records that an update is available and installs nothing.
    install: bool = True
    # The background run: never waits for the lock and never retries a
    # version that already failed.
    background: bool = False
    prefix: Path | None = None
    # The uv to install with; None finds it on PATH.
    uv: str | None = None
    imports: Sequence[str] = IMPORTS
    runner: Runner = run
    total_timeout: float = update.TOTAL_TIMEOUT


def _record(outcome: Outcome, before: str | None, seen: bool) -> None:
    """Save OUTCOME as the last update attempt. The caller holds the lock.

    SEEN says the person watched this run, so no notice repeats it later.
    """
    result = {
        "status": outcome.status,
        "from": before,
        "to": outcome.target,
        "at": update.now(),
        "error": outcome.message if outcome.status == FAILED else None,
        "rolledBack": outcome.rolled_back,
    }
    changes: dict[str, Any] = {"result": result}
    if seen:
        changes["notified"] = f"{outcome.status}:{outcome.target}"
    with contextlib.suppress(OSError):
        write_state(changes)


def _resolve(options: Options) -> update.Release | None:
    """The release to install: `--to`, or the latest. None when sai-cli has
    no release yet."""
    if options.to is not None:
        if update.parse_version(options.to) is None:
            raise UpdateFailed(f"{options.to!r} is not a version like 0.2.0")
        return update.release_for(update.tag_for(options.to), options.web_base)
    try:
        release = update.fetch_latest(
            api_base=options.api_base,
            web_base=options.web_base,
            total_timeout=options.total_timeout,
        )
    except update.NoRelease:
        return None
    except update.UpdateError as exc:
        raise UpdateFailed(f"update check failed: {exc}") from exc
    with contextlib.suppress(OSError):
        write_state({"latest": release.tag, "checkedAt": update.now()})
    return release


def _install(
    options: Options,
    install: update.Install,
    installed: str,
    release: update.Release,
    uv: str,
) -> Outcome:
    """Install RELEASE over INSTALL, verify it, and roll back on failure."""
    prefix, target = Path(install.prefix), release.version
    runner, imports = options.runner, options.imports
    rollback = previous_spec(install, installed)

    def failed(reason: str) -> Outcome:
        """Report REASON after making sure the previous version still runs."""
        if verify(prefix, installed, imports, runner) is None:
            message = f"{reason}. sai {installed} is unchanged."
            return Outcome(FAILED, message, installed, target)
        if rollback is None:
            error = "no previous install is recorded"
        else:
            error = install_spec(uv, rollback, runner) or verify(
                prefix, installed, imports, runner
            )
        if error is None:
            message = f"{reason}. Rolled back to sai {installed}."
            return Outcome(FAILED, message, installed, target, rolled_back=True)
        message = (
            f"{reason}. Rolling back to sai {installed} also failed: {error}."
            f" Reinstall with: {update.INSTALL_COMMAND}"
        )
        return Outcome(FAILED, message, installed, target)

    if options.source:
        spec = f"git+{options.git_url}@{release.tag}"
    else:
        cache = update.wheel_cache()
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache, prefix=".download-") as temp:
            try:
                wheel = fetch_wheel(release, Path(temp))
            except (UpdateFailed, OSError) as exc:
                reason = (
                    str(exc) if isinstance(exc, UpdateFailed) else type(exc).__name__
                )
                message = f"Could not update to {target}: {reason}."
                return Outcome(
                    FAILED,
                    f"{message} sai {installed} is unchanged.",
                    installed,
                    target,
                )
            kept = cache / wheel.name
            os.replace(wheel, kept)
        spec = str(kept)

    error = install_spec(uv, spec, runner)
    if error is None:
        error = verify(prefix, target, imports, runner)
    if error is not None:
        outcome = failed(f"Could not update to {target}: {error}")
        prune_wheels((installed,))
        return outcome
    prune_wheels((installed, target))
    message = (
        f"Updated sai from {installed} to {target}. Restart agent clients and"
        " any open `sai` shell so they load it."
    )
    return Outcome(UPDATED, message, target, target)


def update_now(options: Options | None = None) -> Outcome:
    """Check for a release and install it. Never raises for an update error;
    the outcome carries it."""
    options = options or Options()
    install = update.install_kind(options.prefix, options.web_base)
    prefix = Path(install.prefix)
    if not install.official:
        guide = update.guidance(install)
        note = guide.note + (f" {guide.command}" if guide.command else "")
        message = f"`sai update` only updates an official uv tool install. {note}"
        return Outcome(UNSUPPORTED, message, update.installed_version(prefix))
    with locked(0.0 if options.background else LOCK_WAIT) as held:
        if not held:
            message = "Another sai update is still running; try again shortly."
            status = BUSY if options.background else FAILED
            return Outcome(status, message, update.installed_version(prefix))
        # Read after taking the lock: another update may have just finished.
        installed = update.installed_version(prefix)
        if installed is None:
            return Outcome(FAILED, "Could not read the installed sai version.")
        try:
            release = _resolve(options)
        except UpdateFailed as exc:
            # A failed lookup is not an update attempt, so it is not recorded.
            return Outcome(FAILED, f"{exc}.", installed, options.to)
        if release is None:
            message = "No sai-cli release is published yet; nothing to update to."
            return Outcome(NO_RELEASE, message, installed)
        target = release.version
        if target == installed:
            return Outcome(
                CURRENT, f"sai {installed} is up to date.", installed, target
            )
        if options.to is None and not update.is_newer(release.tag, installed):
            message = (
                f"sai {installed} is newer than the latest release {target};"
                f" nothing to do. `sai update --to {target}` installs it anyway."
            )
            return Outcome(CURRENT, message, installed, target)
        if not options.install:
            message = f"sai {target} is available. Run `sai update` to install it."
            outcome = Outcome(AVAILABLE, message, installed, target)
            _record(outcome, installed, seen=not options.background)
            return outcome
        last = read_state().get("result")
        if (
            options.background
            and isinstance(last, dict)
            and (last.get("status"), last.get("to")) == (FAILED, target)
        ):
            message = f"The update to {target} already failed; run `sai update`."
            return Outcome(FAILED, message, installed, target)
        uv = options.uv or find_uv()
        if uv is None:
            outcome = Outcome(
                FAILED, "uv is not on PATH, so sai cannot update.", installed, target
            )
        else:
            outcome = _install(options, install, installed, release, uv)
        _record(outcome, installed, seen=not options.background)
        return outcome


# Automatic updates


def notice(state: dict[str, Any], installed: str | None) -> tuple[str, str] | None:
    """Return (key, text) of the notice STATE calls for, or None.

    The key names the state, so each state is announced once.
    """
    result = state.get("result")
    if not isinstance(result, dict):
        return None
    status, target = result.get("status"), result.get("to")
    if not isinstance(target, str) or update.parse_version(target) is None:
        return None
    key = f"{status}:{target}"
    if state.get("notified") == key:
        return None
    if status == UPDATED:
        return key, (
            f"sai updated itself to {target}. Restart agent clients so they load it."
        )
    if installed is not None and not update.is_newer(update.tag_for(target), installed):
        return None
    if status == AVAILABLE:
        return key, f"sai {target} is available. Run `sai update` to install it."
    if status == FAILED:
        return key, (
            f"sai could not update itself to {target}. Run `sai update` to retry,"
            " or `sai doctor` for the reason."
        )
    return None


def due(now: float | None = None) -> bool:
    """True when the last background check started over a day ago, or never."""
    try:
        started = stamp_path().stat().st_mtime
    except OSError:
        return True
    return (time.time() if now is None else now) - started >= CHECK_INTERVAL


def stamp() -> None:
    """Record that a background check starts now."""
    path = stamp_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    os.utime(path)


def background_command() -> list[str]:
    return [sys.executable, "-m", "sai_cli", "update", "--background"]


def spawn(command: Sequence[str]) -> None:
    """Start COMMAND detached, so it outlives this process and shares no
    terminal with it."""
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(list(command), **options)


def tick(
    echo: Callable[[str], None],
    start: Callable[[Sequence[str]], None] = spawn,
    prefix: Path | None = None,
) -> None:
    """Run on an interactive command: print a pending notice once, and start
    the daily background update when it is due.

    It never raises and never waits on the network; the command the person
    ran comes first.
    """
    try:
        state = read_state()
        pending = notice(state, update.installed_version(prefix))
        if pending is not None:
            with locked() as held:
                # While an update holds the lock the state is about to
                # change; the next command announces the result.
                if held:
                    echo(pending[1])
                    write_state({"notified": pending[0]})
        if due() and update.install_kind(prefix).official:
            stamp()
            start(background_command())
    except Exception:  # noqa: BLE001 - an update problem must not break a command
        return
