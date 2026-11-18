"""Register `sai mcp serve` with agent clients: Claude Code, Codex, Cursor.

Claude Code and Codex change through their own `mcp add` and `mcp remove`
commands. Cursor has no such command, so this module edits its `mcp.json`.

Client config files hold other servers' secrets. Reads keep only the `sai`
entry's command, args, and `SAI_HOME`; nothing here prints other entries,
env values, headers, or URLs.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME = "sai"
CLIENTS = ("claude", "codex", "cursor")
ALIASES = {"claude-code": "claude", "claude_code": "claude"}
SCOPES = ("user", "project")
SUBPROCESS_TIMEOUT = 60
INSTALL_URL = "git+https://github.com/SaiDotFun/open-sai"


class InstallError(Exception):
    """A refusal that applies to every client, such as an unstable path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    """What a client should launch: the server argv and its environment."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Registration:
    """The `sai` entry found in a client: its argv and SAI_HOME only."""

    argv: list[str]
    scope: str
    sai_home: str | None = None

    def matches(self, target: Target) -> bool:
        return self.argv == target.argv and self.sai_home == target.env.get("SAI_HOME")


@dataclass
class Result:
    client: str
    action: str
    command: list[str] | None = None
    argv: list[list[str]] | None = None
    path: str | None = None
    would: str | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"client": self.client, "action": self.action}
        for key in ("would", "command", "argv", "path", "error"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


class ClientError(Exception):
    """A client command or config file failed; the message is safe to print."""


def normalize_client(name: str) -> str:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in CLIENTS:
        raise ValueError(f"unknown client {name!r}; choose from {', '.join(CLIENTS)}")
    return key


# --- server command -------------------------------------------------------


def uv_cache_dirs(environ: Mapping[str, str], home: Path) -> list[Path]:
    """Directories uv may use as its cache, where `uvx` installs live."""
    dirs = []
    if environ.get("UV_CACHE_DIR"):
        dirs.append(Path(environ["UV_CACHE_DIR"]))
    if environ.get("XDG_CACHE_HOME"):
        dirs.append(Path(environ["XDG_CACHE_HOME"]) / "uv")
    if environ.get("LOCALAPPDATA"):
        dirs.append(Path(environ["LOCALAPPDATA"]) / "uv" / "cache")
    dirs += [home / ".cache" / "uv", home / "Library" / "Caches" / "uv"]
    return dirs


def inside(path: Path, roots: list[Path]) -> bool:
    candidates = {Path(os.path.abspath(path)), Path(os.path.realpath(path))}
    for root in roots:
        for base in {Path(os.path.abspath(root)), Path(os.path.realpath(root))}:
            if any(p.is_relative_to(base) for p in candidates):
                return True
    return False


def server_target(
    override: list[str] | None,
    environ: Mapping[str, str],
    home: Path,
    running_prefix: str,
) -> tuple[Target, list[str]]:
    """Choose the argv clients launch, and return warnings for stderr.

    With no override, register the unresolved `sai` on PATH, normally the
    `~/.local/bin/sai` link that `uv tool` keeps across updates.
    """
    env = {}
    if environ.get("SAI_HOME"):
        env["SAI_HOME"] = os.path.abspath(os.path.expanduser(environ["SAI_HOME"]))
    if override:
        return Target(list(override), env), []
    found = shutil.which("sai", path=environ.get("PATH"))
    if found is None:
        raise InstallError(
            "NOT_ON_PATH",
            "No `sai` on PATH to register. Install it with"
            f" `uv tool install {INSTALL_URL}`, or pass the server command"
            " after `--`.",
        )
    path = Path(os.path.abspath(found))
    if inside(path, uv_cache_dirs(environ, home)):
        raise InstallError(
            "UNSTABLE_PATH",
            f"The `sai` on PATH ({path}) is inside the uv cache, which uv"
            " cleans up, so a client registered with it would break. Install"
            f" sai first with `uv tool install {INSTALL_URL}`, then rerun this"
            " command.",
        )
    warnings = []
    if not inside(path, [Path(running_prefix)]):
        warnings.append(
            f"The `sai` on PATH ({path}) is a different install than the one"
            f" running ({running_prefix}); clients get the one on PATH."
        )
    return Target([str(path), "mcp", "serve"], env), warnings


# --- helpers --------------------------------------------------------------


def run(argv: list[str]) -> None:
    """Run a client command; raise ClientError with its first stderr line."""
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientError(f"`{Path(argv[0]).name}` did not run: {type(exc).__name__}")
    if done.returncode != 0:
        raise ClientError(
            f"`{Path(argv[0]).name} {' '.join(argv[1:3])}` exited"
            f" {done.returncode}: {first_line(done.stderr or done.stdout)}"
        )


def first_line(text: str) -> str:
    """Pick the error line from client output; clients also print warnings."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    chosen = errors[0] if errors else (lines[-1] if lines else "no output")
    return chosen[:200]


def load_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object file; None when missing. Never echoes contents."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ClientError(f"could not read {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise ClientError(f"could not read {path}: not a JSON object")
    return data


def entry_registration(entry: Any, scope: str) -> Registration | None:
    """Keep only command, args, and SAI_HOME from a client's `sai` entry."""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    args = entry.get("args") or []
    argv = [command] if isinstance(command, str) else []
    argv += [a for a in args if isinstance(a, str)] if isinstance(args, list) else []
    env = entry.get("env")
    home = env.get("SAI_HOME") if isinstance(env, dict) else None
    return Registration(argv, scope, home if isinstance(home, str) else None)


def servers(data: dict[str, Any] | None) -> dict[str, Any]:
    found = (data or {}).get("mcpServers")
    return found if isinstance(found, dict) else {}


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Replace path in one rename, keeping its mode (0600 for a new file)."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


# --- clients --------------------------------------------------------------


class Client:
    name = ""
    binary = ""

    def __init__(self, home: Path, environ: Mapping[str, str], cwd: Path) -> None:
        self.home = home
        self.environ = environ
        self.cwd = cwd

    def exe(self) -> str | None:
        if not self.binary:
            return None
        return shutil.which(self.binary, path=self.environ.get("PATH"))

    def home_dir(self) -> Path:
        raise NotImplementedError

    def detected(self) -> tuple[bool, str | None]:
        if self.exe():
            return True, "cli"
        if self.home_dir().is_dir():
            return True, "home"
        return False, None

    def require_exe(self) -> str:
        exe = self.exe()
        if exe is None:
            raise ClientError(f"`{self.binary}` is not on PATH")
        return exe

    def registrations(self) -> list[Registration]:
        """Every `sai` entry, most specific scope last."""
        raise NotImplementedError

    def registration(self, scope: str) -> Registration | None:
        return next((r for r in self.registrations() if r.scope == scope), None)

    def add_steps(self, scope: str, target: Target) -> list[list[str]]:
        raise NotImplementedError

    def remove_steps(self, scope: str) -> list[list[str]]:
        raise NotImplementedError

    def apply(self, steps: list[list[str]]) -> None:
        for argv in steps:
            run(argv)


class Claude(Client):
    name = "claude"
    binary = "claude"

    def home_dir(self) -> Path:
        configured = self.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured) if configured else self.home / ".claude"

    def config_path(self) -> Path:
        configured = self.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(configured) if configured else self.home) / ".claude.json"

    def registrations(self) -> list[Registration]:
        # Never `claude mcp get`: it starts the server to check its health.
        data = load_json(self.config_path())
        found = []
        user = servers(data).get(NAME)
        if user is not None:
            found.append(entry_registration(user, "user"))
        projects = (data or {}).get("projects")
        project = projects.get(str(self.cwd)) if isinstance(projects, dict) else None
        local = servers(project if isinstance(project, dict) else None).get(NAME)
        if local is not None:
            found.append(entry_registration(local, "local"))
        shared = servers(load_json(self.cwd / ".mcp.json")).get(NAME)
        if shared is not None:
            found.append(entry_registration(shared, "project"))
        return [r for r in found if r is not None]

    def add_steps(self, scope: str, target: Target) -> list[list[str]]:
        exe = self.require_exe()
        env = [f"--env={k}={v}" for k, v in target.env.items()]
        return [[exe, "mcp", "add", "--scope", scope, NAME, *env, "--", *target.argv]]

    def remove_steps(self, scope: str) -> list[list[str]]:
        return [[self.require_exe(), "mcp", "remove", "--scope", scope, NAME]]


class Codex(Client):
    name = "codex"
    binary = "codex"

    def home_dir(self) -> Path:
        configured = self.environ.get("CODEX_HOME")
        return Path(configured) if configured else self.home / ".codex"

    def registrations(self) -> list[Registration]:
        exe = self.exe()
        if exe is None:
            return []
        try:
            done = subprocess.run(
                [exe, "mcp", "get", NAME, "--json"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClientError(f"`codex mcp get` did not run: {type(exc).__name__}")
        if done.returncode != 0:
            if "no mcp server" in (done.stderr + done.stdout).lower():
                return []
            raise ClientError(
                f"`codex mcp get` exited {done.returncode}:"
                f" {first_line(done.stderr or done.stdout)}"
            )
        try:
            data = json.loads(done.stdout)
        except ValueError:
            raise ClientError("`codex mcp get` printed something other than JSON")
        transport = data.get("transport") if isinstance(data, dict) else None
        found = entry_registration(transport, "user")
        return [found] if found is not None else []

    def check_scope(self, scope: str) -> None:
        if scope != "user":
            raise ClientError("Codex registers MCP servers per user only")

    def add_steps(self, scope: str, target: Target) -> list[list[str]]:
        self.check_scope(scope)
        env = [f"--env={k}={v}" for k, v in target.env.items()]
        return [[self.require_exe(), "mcp", "add", NAME, *env, "--", *target.argv]]

    def remove_steps(self, scope: str) -> list[list[str]]:
        self.check_scope(scope)
        return [[self.require_exe(), "mcp", "remove", NAME]]


class Cursor(Client):
    name = "cursor"
    binary = "cursor-agent"
    HINT = "You may need to enable sai in Cursor under Cursor Settings > MCP."

    def home_dir(self) -> Path:
        return self.home / ".cursor"

    def detected(self) -> tuple[bool, str | None]:
        # Cursor's editor CLI and `cursor-agent` both read ~/.cursor/mcp.json.
        if self.exe() or shutil.which("cursor", path=self.environ.get("PATH")):
            return True, "cli"
        if self.home_dir().is_dir():
            return True, "home"
        return False, None

    def config_path(self, scope: str) -> Path:
        base = self.cwd if scope == "project" else self.home
        return base / ".cursor" / "mcp.json"

    def registrations(self) -> list[Registration]:
        found = []
        for scope in SCOPES:
            entry = servers(load_json(self.config_path(scope))).get(NAME)
            if entry is not None:
                reg = entry_registration(entry, scope)
                if reg is not None:
                    found.append(reg)
        return found

    def write(self, scope: str, target: Target | None) -> None:
        path = self.config_path(scope)
        data = load_json(path) or {}
        entries = data.get("mcpServers")
        if not isinstance(entries, dict):
            entries = {}
        if target is None:
            entries.pop(NAME, None)
        else:
            entry: dict[str, Any] = {
                "command": target.argv[0],
                "args": target.argv[1:],
            }
            if target.env:
                entry["env"] = dict(target.env)
            entries[NAME] = entry
        data["mcpServers"] = entries
        write_json_atomic(path, data)


def make_clients(
    home: Path, environ: Mapping[str, str], cwd: Path
) -> dict[str, Client]:
    return {
        "claude": Claude(home, environ, cwd),
        "codex": Codex(home, environ, cwd),
        "cursor": Cursor(home, environ, cwd),
    }


# --- commands -------------------------------------------------------------


def list_clients(
    clients: dict[str, Client], target: Target | None
) -> list[dict[str, Any]]:
    rows = []
    for name, client in clients.items():
        detected, via = client.detected()
        row: dict[str, Any] = {
            "client": name,
            "detected": detected,
            "via": via,
            "registered": False,
            "current": False,
            "registeredCommand": None,
            "scope": None,
        }
        try:
            found = client.registrations()
        except ClientError as exc:
            row["error"] = str(exc)
            found = []
        if found:
            reg = found[0]
            row.update(
                registered=True,
                current=target is not None and reg.matches(target),
                registeredCommand=reg.argv,
                scope=reg.scope,
            )
        rows.append(row)
    return rows


def install_one(client: Client, scope: str, target: Target, dry_run: bool) -> Result:
    result = Result(client.name, "failed", command=target.argv)
    try:
        existing = client.registration(scope)
        if existing is not None and existing.matches(target):
            result.action = "unchanged"
            return result
        action = "added" if existing is None else "replaced"
        if isinstance(client, Cursor):
            result.path = str(client.config_path(scope))
            steps: list[list[str]] = []
        else:
            steps = [] if existing is None else client.remove_steps(scope)
            steps += client.add_steps(scope, target)
            result.argv = steps
        if dry_run:
            result.action, result.would = "planned", action
            return result
        if isinstance(client, Cursor):
            client.write(scope, target)
        else:
            client.apply(steps)
        result.action = action
    except (ClientError, OSError) as exc:
        result.action, result.error = "failed", str(exc)
    return result


def uninstall_one(client: Client, scope: str, dry_run: bool) -> Result:
    result = Result(client.name, "failed")
    try:
        existing = client.registration(scope)
        if existing is None:
            result.action = "absent"
            return result
        result.command = existing.argv
        if isinstance(client, Cursor):
            result.path = str(client.config_path(scope))
            steps: list[list[str]] = []
        else:
            steps = client.remove_steps(scope)
            result.argv = steps
        if dry_run:
            result.action, result.would = "planned", "removed"
            return result
        if isinstance(client, Cursor):
            client.write(scope, None)
        else:
            client.apply(steps)
        result.action = "removed"
    except (ClientError, OSError) as exc:
        result.action, result.error = "failed", str(exc)
    return result


def summarize(results: list[Result]) -> dict[str, Any]:
    changed = {"added", "replaced", "removed"}
    return {
        "results": [r.to_json() for r in results],
        "reconnectNeeded": any(r.action in changed for r in results),
    }


def running_prefix() -> str:
    return sys.prefix
