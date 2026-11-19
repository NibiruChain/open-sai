"""Install a sai-cli release over this install, verify it, and roll back.

`sai update` downloads the release wheel and its SHA256SUMS, checks the hash,
installs the wheel with `uv tool install --force`, and then runs the new
`sai --version` and imports the package. A failure at any step leaves or
restores the previous version. Only an official uv tool install is changed.

Interactive commands call `tick`, which prints at most one notice per state
change and starts a detached `sai update --background` at most once a day.

Two locks keep this safe. The tool lock sits beside the uv tool environment
and covers a whole install, verify, and rollback, so every SAI_HOME that
shares the tool takes the same one. The state lock is `$SAI_HOME/update.lock`
and covers each read-modify-write of that profile's `update.json`.

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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Generator, Iterator, Sequence
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
# How long a write to update.json waits for the state lock.
STATE_WAIT = 5.0
# The background check runs at most this often.
CHECK_INTERVAL = 24 * 60 * 60
# A background run that was started but never began work is retried after
# this long instead of a day later.
RETRY_INTERVAL = 15 * 60
TOOL_LOCK_NAME = ".sai-cli-update.lock"
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


# State and locks


def state_lock_path() -> Path:
    """The lock for this profile's update.json."""
    return update.sai_home() / "update.lock"


def tool_lock_path(prefix: Path) -> Path:
    """The lock for installing over the uv tool environment at PREFIX.

    It sits beside the environment, in uv's tool directory, so commands with
    different SAI_HOME values that share the tool take the same lock, and uv
    recreating the environment does not remove it.
    """
    return prefix.parent / TOOL_LOCK_NAME


def read_state(path: Path | None = None) -> dict[str, Any]:
    """Return update.json as a dict, or {} when it is missing or unreadable.

    `latest` and `checkedAt` are the last check. `result` is the last update
    attempt and `notified` the notice already shown for it. `scheduledAt` is
    when a command last started the background run and `startedAt` when that
    run last began work.
    """
    try:
        data = json.loads((path or update.cache_path()).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != update.CACHE_SCHEMA:
        return {}
    return data


def write_state(changes: dict[str, Any], path: Path | None = None) -> None:
    """Merge CHANGES into update.json atomically. Callers hold the state
    lock."""
    target = path or update.cache_path()
    state = {**read_state(target), **changes, "schema": update.CACHE_SCHEMA}
    update.write_json(target, state)


def save_state(changes: dict[str, Any]) -> bool:
    """Take the state lock and merge CHANGES into update.json. Return False
    when the lock or the file could not be had; state is best effort."""
    try:
        with locked(wait=STATE_WAIT) as held:
            if held:
                write_state(changes)
            return held
    except OSError:
        return False


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
def locked(path: Path | None = None, wait: float = 0.0) -> Generator[bool]:
    """Hold the lock file at PATH (default: the state lock). Yields False
    when another process still holds it after WAIT seconds."""
    path = path or state_lock_path()
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


def fetch_wheel(
    release: update.Release, directory: Path, timeout: float = DOWNLOAD_TIMEOUT
) -> tuple[Path, str]:
    """Download RELEASE's wheel and SHA256SUMS into DIRECTORY and return the
    wheel and its SHA-256, or raise UpdateFailed when the hash differs.

    TIMEOUT is wall-clock time for both transfers, with their redirects and
    response headers. HTTPX timeouts apply per network operation, so the
    transfers run in a daemon thread that this stops waiting for at the
    deadline, as the release lookup does.
    """
    name = update.wheel_name(release.version)
    wheel, sums = directory / name, directory / "SHA256SUMS"
    deadline = time.monotonic() + timeout
    outcome: dict[str, Exception] = {}

    def work() -> None:
        try:
            headers = {"User-Agent": "sai-cli"}
            with httpx.Client(headers=headers, follow_redirects=True) as client:
                _download(client, release.sums_url, sums, MAX_SUMS, deadline)
                _download(client, release.wheel_url, wheel, MAX_WHEEL, deadline)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="sai-update-download", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise UpdateFailed(f"timed out downloading {name}")
    error = outcome.get("error")
    if isinstance(error, UpdateFailed):
        raise error
    if isinstance(error, httpx.HTTPError):
        raise UpdateFailed(f"download failed: {type(error).__name__}") from error
    if error is not None:
        raise error
    expected = expected_hash(sums.read_text(errors="replace"), name)
    if expected is None:
        raise UpdateFailed(f"SHA256SUMS does not list {name}")
    actual = sha256(wheel)
    if actual != expected:
        raise UpdateFailed(f"{name} does not match its SHA256SUMS entry")
    return wheel, actual


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


def _python(prefix: Path) -> Path:
    """The interpreter of the environment at PREFIX."""
    if os.name == "nt":
        return prefix / "Scripts" / "python.exe"
    return prefix / "bin" / "python"


def registered_launcher(prefix: Path) -> Path | None:
    """The `sai` executable uv installed for the tool at PREFIX, read from
    its receipt. This is the path MCP clients are registered with."""
    try:
        with (prefix / "uv-receipt.toml").open("rb") as handle:
            receipt = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    entries = receipt.get("tool", {}).get("entrypoints", [])
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("name") in ("sai", "sai.exe"):
            path = entry.get("install-path")
            if isinstance(path, str):
                return Path(path)
    return None


def _same(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def destination_error(prefix: Path, uv: str, runner: Runner) -> str | None:
    """Return why `uv tool install` would not replace the install at PREFIX,
    or None when it would.

    uv installs into the tool and executable directories of its current
    settings. If those are not where this install lives, an update would
    change some other install and leave this one as it is.
    """
    launcher = registered_launcher(prefix)
    if launcher is None:
        return "this install's uv receipt names no `sai` executable"
    found: list[Path] = []
    for extra in ((), ("--bin",)):
        code, output = runner([uv, "tool", "dir", *extra], VERIFY_TIMEOUT)
        if code != 0:
            return f"`uv tool dir` failed: {_last_line(output)}"
        found.append(Path(_last_line(output)))
    tools, executables = found
    if not _same(tools, prefix.parent):
        return f"uv installs tools into {tools}, but this sai is installed at {prefix}"
    if not _same(executables, launcher.parent):
        return (
            f"uv puts executables in {executables}, but this install's `sai`"
            f" is {launcher}"
        )
    return None


def verify(
    prefix: Path, version: str, imports: Sequence[str], runner: Runner
) -> str | None:
    """Return why the install at PREFIX is not a working VERSION, or None.

    It runs the registered `sai`, the one on PATH that clients use, and
    imports the package with the environment's interpreter.
    """
    launcher = registered_launcher(prefix)
    if launcher is None:
        return "the install's uv receipt names no `sai` executable"
    code, output = runner([str(launcher), "--version"], VERIFY_TIMEOUT)
    if code != 0:
        return f"`sai --version` failed: {_last_line(output)}"
    if not output.strip().endswith(f" {version}"):
        return f"`sai --version` printed {_last_line(output)!r}, not {version}"
    statement = "import " + ", ".join(imports)
    code, output = runner([str(_python(prefix)), "-c", statement], VERIFY_TIMEOUT)
    if code != 0:
        return f"the package does not import: {_last_line(output)}"
    return None


def install_spec(uv: str, spec: str, runner: Runner) -> str | None:
    """Run `uv tool install --force SPEC`; return the error, or None."""
    code, output = runner([uv, "tool", "install", "--force", spec], UV_TIMEOUT)
    return None if code == 0 else f"uv tool install failed: {_last_line(output)}"


def write_record(prefix: Path, tag: str, digest: str, wheel: Path) -> None:
    """Record that the install at PREFIX is WHEEL of release TAG, whose
    SHA-256 DIGEST matched the release's SHA256SUMS."""
    installed = update.installed_files_hash(prefix)
    if installed is None:
        raise OSError("the install has no RECORD file")
    record = update.ReleaseRecord(tag, digest, installed, str(wheel))
    update.write_json(update.release_record_path(prefix), record.to_json())


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
    # Asked right before installing; False records that an update is
    # available and installs nothing. The background run passes the
    # update.auto setting, read fresh, so an opt-out made meanwhile holds.
    allowed: Callable[[], bool] | None = None
    # The background run: never waits for the lock and never retries a
    # version that already failed.
    background: bool = False
    prefix: Path | None = None
    # The uv to install with; None finds it on PATH.
    uv: str | None = None
    imports: Sequence[str] = IMPORTS
    runner: Runner = run
    total_timeout: float = update.TOTAL_TIMEOUT
    download_timeout: float = DOWNLOAD_TIMEOUT


def _record(outcome: Outcome, before: str | None, seen: bool) -> None:
    """Save OUTCOME as the last update attempt.

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
    save_state(changes)


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
        save_state({"latest": None, "checkedAt": update.now()})
        return None
    except update.UpdateError as exc:
        raise UpdateFailed(f"update check failed: {exc}") from exc
    save_state({"latest": release.tag, "checkedAt": update.now()})
    return release


@dataclass(frozen=True)
class Current:
    """The install an update starts from, read under the tool lock."""

    install: update.Install
    version: str
    # The release record, when the install came from a verified wheel.
    record: update.ReleaseRecord | None

    @property
    def prefix(self) -> Path:
        return Path(self.install.prefix)


def _download_to_cache(release: update.Release, options: Options) -> tuple[Path, str]:
    """Download and verify RELEASE's wheel and keep it in the wheel cache."""
    cache = update.wheel_cache()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix=".download-") as temp:
        wheel, digest = fetch_wheel(release, Path(temp), options.download_timeout)
        kept = cache / wheel.name
        os.replace(wheel, kept)
    return kept, digest


def rollback_sources(
    current: Current, options: Options
) -> Iterator[tuple[str, tuple[str, str, Path] | None]]:
    """Yield what can put CURRENT's version back, most trusted first, as
    (spec for uv, (tag, sha256, wheel) for a wheel or None).

    A kept wheel counts only while its bytes still hash to what the release
    record says. Otherwise the release is downloaded and checked against its
    SHA256SUMS again. A Git install goes back to its recorded commit.
    """
    record, install = current.record, current.install
    tag = update.tag_for(current.version)
    if record is not None and record.wheel:
        kept = Path(record.wheel)
        try:
            intact = kept.is_file() and sha256(kept) == record.sha256
        except OSError:
            intact = False
        if intact:
            yield str(kept), (record.tag, record.sha256, kept)
    if install.kind == update.UV_TOOL_RELEASE:
        try:
            release = update.release_for(tag, options.web_base)
            wheel, digest = _download_to_cache(release, options)
        except (UpdateFailed, OSError):
            pass
        else:
            yield str(wheel), (tag, digest, wheel)
    if install.kind == update.UV_TOOL_GIT and install.url and install.commit:
        yield f"git+{install.url}@{install.commit}", None


def restore(current: Current, options: Options, uv: str) -> str | None:
    """Put CURRENT's version back and verify it. Return None when it runs
    again, or the last error."""
    error = "no verified copy of the previous version is available"
    for spec, wheel in rollback_sources(current, options):
        error = install_spec(uv, spec, options.runner) or verify(
            current.prefix, current.version, options.imports, options.runner
        )
        if error is None:
            if wheel is not None:
                with contextlib.suppress(OSError):
                    write_record(current.prefix, *wheel)
            return None
    return error


@contextlib.contextmanager
def _uninterrupted() -> Generator[None]:
    """Ignore Ctrl-C while a rollback runs. Child processes inherit the
    ignored signal, so the terminal's interrupt does not stop uv either."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _install(
    options: Options, current: Current, release: update.Release, uv: str
) -> tuple[Outcome, bool]:
    """Install RELEASE over CURRENT, verify it, and roll back on failure.

    Returns the outcome and whether Ctrl-C interrupted the run. An interrupt
    after uv started replacing files still ends with a working install.
    """
    prefix, installed, target = current.prefix, current.version, release.version
    runner, imports = options.runner, options.imports

    def failed(reason: str) -> Outcome:
        """Report REASON after making sure the previous version still runs."""
        if verify(prefix, installed, imports, runner) is None:
            message = f"{reason}. sai {installed} is unchanged."
            return Outcome(FAILED, message, installed, target)
        error = restore(current, options, uv)
        if error is None:
            message = f"{reason}. Rolled back to sai {installed}."
            return Outcome(FAILED, message, installed, target, rolled_back=True)
        message = (
            f"{reason}. Rolling back to sai {installed} also failed: {error}."
            f" Reinstall with: {update.INSTALL_COMMAND}"
        )
        return Outcome(FAILED, message, installed, target)

    wheel: tuple[str, str, Path] | None = None
    if options.source:
        # The full ref: a branch named like the tag must not shadow it.
        spec = f"git+{options.git_url}@refs/tags/{release.tag}"
    else:
        try:
            kept, digest = _download_to_cache(release, options)
        except (UpdateFailed, OSError) as exc:
            reason = str(exc) if isinstance(exc, UpdateFailed) else type(exc).__name__
            message = f"Could not update to {target}: {reason}."
            return (
                Outcome(
                    FAILED,
                    f"{message} sai {installed} is unchanged.",
                    installed,
                    target,
                ),
                False,
            )
        spec, wheel = str(kept), (release.tag, digest, kept)

    interrupted = False
    try:
        error = install_spec(uv, spec, runner)
        if error is None:
            error = verify(prefix, target, imports, runner)
        if error is None and wheel is not None:
            try:
                write_record(prefix, *wheel)
            except OSError as exc:
                error = f"could not record the release: {type(exc).__name__}"
    except KeyboardInterrupt:
        interrupted = True
        error = "interrupted"
    if error is not None:
        with _uninterrupted():
            outcome = failed(f"Could not update to {target}: {error}")
            prune_wheels((installed,))
        return outcome, interrupted
    if wheel is None:
        # A Git install has no release record; drop a stale one.
        with contextlib.suppress(OSError):
            update.release_record_path(prefix).unlink(missing_ok=True)
    prune_wheels((installed, target))
    message = (
        f"Updated sai from {installed} to {target}. Restart agent clients and"
        " any open `sai` shell so they load it."
    )
    return Outcome(UPDATED, message, target, target), False


def _unsupported(install: update.Install) -> Outcome:
    guide = update.guidance(install)
    note = guide.note + (f" {guide.command}" if guide.command else "")
    message = f"`sai update` only updates an official uv tool install. {note}"
    version = update.installed_version(Path(install.prefix))
    return Outcome(UNSUPPORTED, message, version)


def update_now(options: Options | None = None) -> Outcome:
    """Check for a release and install it. Never raises for an update error;
    the outcome carries it. Ctrl-C is raised again after any rollback."""
    options = options or Options()
    install = update.install_kind(options.prefix, options.web_base)
    if not install.official:
        return _unsupported(install)
    prefix = Path(install.prefix)
    wait = 0.0 if options.background else LOCK_WAIT
    with locked(tool_lock_path(prefix), wait) as held:
        if not held:
            message = "Another sai update is still running; try again shortly."
            status = BUSY if options.background else FAILED
            return Outcome(status, message, update.installed_version(prefix))
        # Everything about the install is read after taking the lock: an
        # update that just finished may have changed its version and origin.
        install = update.install_kind(options.prefix, options.web_base)
        if not install.official:
            return _unsupported(install)
        installed = update.installed_version(prefix)
        if installed is None:
            return Outcome(FAILED, "Could not read the installed sai version.")
        current = Current(install, installed, update.read_release_record(prefix))
        if options.background:
            save_state({"startedAt": time.time()})
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
        seen = not options.background
        if options.allowed is not None and not options.allowed():
            message = f"sai {target} is available. Run `sai update` to install it."
            outcome = Outcome(AVAILABLE, message, installed, target)
            _record(outcome, installed, seen)
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
            _record(outcome, installed, seen)
            return outcome
        mismatch = destination_error(prefix, uv, options.runner)
        if mismatch is not None:
            message = (
                f"`sai update` stopped before changing anything: {mismatch}."
                " Run it with the uv settings this sai was installed with."
            )
            return Outcome(UNSUPPORTED, message, installed, target)
        outcome, interrupted = _install(options, current, release, uv)
        _record(outcome, installed, seen)
        if interrupted:
            raise KeyboardInterrupt
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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def due(state: dict[str, Any], now: float | None = None) -> bool:
    """True when a background run should start.

    That is a day after the last one was started, or never. A run that was
    started but never began work, because the process did not start or an
    update held the tool lock, is retried after RETRY_INTERVAL.
    """
    scheduled, started = (
        _number(state.get("scheduledAt")),
        _number(state.get("startedAt")),
    )
    if scheduled is None:
        return True
    age = (time.time() if now is None else now) - scheduled
    if age >= CHECK_INTERVAL:
        return True
    began = started is not None and started >= scheduled
    return not began and age >= RETRY_INTERVAL


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

    An install `sai update` does not change gets neither. The notice is
    read, printed, and marked under the state lock, and the daily run is
    reserved under it, so two terminals neither repeat a notice nor start two
    runs. It never raises and never waits on the network; the command the
    person ran comes first.
    """
    try:
        if not update.install_kind(prefix).official:
            return
        launch = False
        with locked() as held:
            if not held:
                # Another command is doing this right now.
                return
            state = read_state()
            changes: dict[str, Any] = {}
            pending = notice(state, update.installed_version(prefix))
            if pending is not None:
                echo(pending[1])
                changes["notified"] = pending[0]
            if due(state):
                changes["scheduledAt"] = time.time()
                launch = True
            if changes:
                write_state(changes)
        if launch:
            start(background_command())
    except Exception:  # noqa: BLE001 - an update problem must not break a command
        return
