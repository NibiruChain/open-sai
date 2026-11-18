"""Structured logs: one JSON object per line under $SAI_HOME/logs.

Each surface writes its own file per UTC day, `<surface>-YYYY-MM-DD.jsonl`,
where the surface is `cli`, `mcp`, or `update`. The directory has mode 0700
and the files 0600. Every line is one `os.write` on a descriptor opened with
`O_APPEND`, so lines from concurrent processes never interleave.

Every line carries `ts`, `level`, `event`, `surface`, `pid`, `version`,
`runId` (one per process), and `seq` (the event's number within the run,
which orders events that share a millisecond), then the fields bound with
`bind`, then the event's own fields.

Callers pass only values that are safe to store: never the session token, a
signature, or clipboard text. As a second guard, `event` removes every
stored session token and anything shaped like a JWT from each string, and
callers pass URLs through `url`.

Logging never writes to stdout or stderr and never raises: a logs directory
that cannot be written turns logging off for the rest of the process.
Commands that promise to change nothing, such as `sai doctor --no-fix`, run
inside `silenced` and write no lines.

`logs.enabled` (SAI_LOG) turns logging off; `logs.keep_days`
(SAI_LOG_KEEP_DAYS) sets how many days of files are kept. Old files are
deleted at most once a day, by the first event a process writes.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
import uuid
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any

DIR_NAME = "logs"
SURFACES = ("cli", "mcp", "update")
LEVELS = ("info", "warn", "error")
DIR_MODE, FILE_MODE = 0o700, 0o600
DEFAULT_KEEP_DAYS = 14
PRUNE_MARKER = ".pruned"
PRUNE_INTERVAL_SECONDS = 24 * 60 * 60
# Longest string kept in a field, and longest line written.
MAX_TEXT = 2000
MAX_LINE = 16 * 1024
REDACTED = "[redacted session token]"
COMMON = ("ts", "level", "event", "surface", "pid", "version", "runId", "seq")

RUN_ID = uuid.uuid4().hex
_seq = itertools.count(1)
_FILE = re.compile(r"^(?P<surface>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})\.jsonl$")
# Three base64url segments, the first a JSON object: a JWT such as the
# session token.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "sai_log_context", default=MappingProxyType({})
)
# Set after a failed write, so one unwritable directory costs one attempt.
_broken = False
_pruned: set[Path] = set()
# How many `silenced` blocks are open, in any thread.
_silenced = 0
_config_cache: tuple[Any, tuple[bool, int]] | None = None
_token_cache: tuple[Any, tuple[str, ...]] | None = None


def home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    return Path(values.get("SAI_HOME", Path.home() / ".sai"))


def directory(env: Mapping[str, str] | None = None) -> Path:
    """Where the log files live. Reading this creates nothing."""
    return home(env) / DIR_NAME


def _version() -> str:
    try:
        return metadata.version("sai-cli")
    except metadata.PackageNotFoundError:
        return "unknown"


VERSION = _version()


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def config() -> tuple[bool, int]:
    """Return (`logs.enabled`, `logs.keep_days`) from the environment and
    the config file.

    A config file that cannot be read or parsed leaves the environment and
    the defaults in charge, so a broken file never turns logging into an
    error. The answer is cached until the file or the variables change.
    """
    global _config_cache
    from . import settings

    path = settings.config_path(os.environ)
    names = [settings.BY_KEY[key].env for key in ("logs.enabled", "logs.keep_days")]
    key = (str(path), _stat_key(path), *(os.environ.get(name) for name in names))
    if _config_cache is not None and _config_cache[0] == key:
        return _config_cache[1]
    try:
        resolved = settings.load()
    except settings.SettingsError:
        try:
            resolved = settings.resolve({}, os.environ, {})
        except settings.SettingsError:
            resolved = None
    value = (
        (True, DEFAULT_KEEP_DAYS)
        if resolved is None
        else (resolved.logs_enabled, resolved.logs_keep_days)
    )
    _config_cache = (key, value)
    return value


def enabled() -> bool:
    return config()[0]


# Redaction


def _tokens() -> tuple[str, ...]:
    """Every stored session token, cached until the session file changes."""
    global _token_cache
    from . import storage

    paths = (storage.home() / "sessions.json", storage.home() / "session.json")
    key = tuple(_stat_key(path) for path in paths)
    if _token_cache is not None and _token_cache[0] == key:
        return _token_cache[1]
    try:
        stored = storage.load_accounts()["sessions"].values()
        tokens = tuple(t for s in stored if (t := s.get("token")))
    except (ValueError, KeyError, OSError):
        tokens = ()
    _token_cache = (key, tokens)
    return tokens


def redact(text: str) -> str:
    """Remove every connected account's session token from TEXT."""
    for token in _tokens():
        text = text.replace(token, REDACTED)
    return text


def url(value: str | None) -> str | None:
    """VALUE without user info, query, or fragment; see
    `settings.public_url`."""
    from . import settings

    return settings.public_url(value)


def argument(token: str) -> str:
    """One command-line token, safe to store: a URL, alone or after
    `--option=`, loses its credentials."""
    if "://" not in token:
        return token
    if token.startswith("-") and "=" in token:
        option, _, value = token.partition("=")
        return f"{option}={url(value)}"
    return str(url(token))


def _text(value: str) -> str:
    value = _JWT.sub(REDACTED, redact(value))
    if len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"... [{len(value) - MAX_TEXT} more characters]"
    return value


def clean(value: Any) -> Any:
    """Return VALUE as JSON-ready data with tokens removed and long strings
    cut."""
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {_text(str(key)): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean(item) for item in value]
    return _text(str(value))


# Writing


@contextmanager
def bind(**fields: Any) -> Generator[None]:
    """Add FIELDS to every event written inside the block, in this thread
    and in the threads and tasks it starts. `surface` picks the file."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def silenced() -> Generator[None]:
    """Write no events inside the block, in any thread of this process."""
    global _silenced
    _silenced += 1
    try:
        yield
    finally:
        _silenced -= 1


def bound(name: str) -> Any:
    """The value `bind` gave NAME in this context, or None."""
    return _context.get().get(name)


def now() -> datetime:
    return datetime.now(UTC)


def stamp(instant: datetime) -> str:
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def file_path(surface: str, day: date, root: Path | None = None) -> Path:
    return (root or directory()) / f"{surface}-{day.isoformat()}.jsonl"


def record(name: str, level: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Build the line for event NAME: common fields, bound fields, FIELDS."""
    context = dict(_context.get())
    surface = context.pop("surface", "cli")
    extra = {k: v for k, v in {**context, **fields}.items() if v is not None}
    return {
        "ts": stamp(now()),
        "level": level if level in LEVELS else "info",
        "event": name,
        "surface": surface if surface in SURFACES else "cli",
        "pid": os.getpid(),
        "version": VERSION,
        "runId": RUN_ID,
        "seq": next(_seq),
        **clean(extra),
    }


def encode(line: Mapping[str, Any]) -> bytes:
    """One JSON line. A line over MAX_LINE keeps its common fields and says
    which fields it dropped."""
    data = json.dumps(line, separators=(",", ":"), default=str)
    if len(data.encode()) > MAX_LINE:
        kept = {key: line[key] for key in COMMON if key in line}
        kept["truncated"] = sorted(key for key in line if key not in COMMON)
        data = json.dumps(kept, separators=(",", ":"), default=str)
    return data.encode() + b"\n"


def _ensure_directory(root: Path) -> None:
    if root.is_dir():
        return
    root.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        root.mkdir(mode=DIR_MODE)
    except FileExistsError:
        return
    root.chmod(DIR_MODE)


def append(path: Path, data: bytes) -> None:
    """Append DATA to PATH with one write, creating the file at mode 0600."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, FILE_MODE)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def event(name: str, level: str = "info", /, **fields: Any) -> None:
    """Write one event. Does nothing when logging is off, and never raises."""
    global _broken
    if _broken or _silenced:
        return
    try:
        on, keep_days = config()
        if not on:
            return
        line = record(name, level, fields)
        root = directory()
        _ensure_directory(root)
        append(file_path(line["surface"], now().date(), root), encode(line))
        if root not in _pruned:
            _pruned.add(root)
            prune(root, keep_days)
    except OSError:
        _broken = True
    except Exception:  # noqa: BLE001, S110 - a log line must not break a command
        pass


def reset() -> None:
    """Forget cached state, so the next event rereads the configuration and
    retries a directory that failed. For tests and long-running processes."""
    global _broken, _config_cache, _token_cache, _silenced
    _broken = False
    _silenced = 0
    _config_cache = None
    _token_cache = None
    _pruned.clear()


# Retention


def log_files(root: Path | None = None) -> list[tuple[date, str, Path]]:
    """Every log file under ROOT as (day, surface, path), oldest first."""
    found: list[tuple[date, str, Path]] = []
    try:
        names = sorted(os.listdir(root or directory()))
    except OSError:
        return found
    for name in names:
        match = _FILE.match(name)
        if match is None:
            continue
        try:
            day = date.fromisoformat(match["date"])
        except ValueError:
            continue
        found.append((day, match["surface"], (root or directory()) / name))
    return sorted(found)


def expired_files(
    files: list[tuple[date, str, Path]], today: date, keep_days: int
) -> list[Path]:
    """The files older than KEEP_DAYS days: today and the KEEP_DAYS - 1
    days before it are kept."""
    oldest = today - timedelta(days=max(keep_days, 1) - 1)
    return [path for day, _surface, path in files if day < oldest]


def prune(root: Path, keep_days: int, *, force: bool = False) -> list[Path]:
    """Delete log files older than KEEP_DAYS days, at most once a day.

    The marker file's modification time records the last run across
    processes. Returns the files deleted.
    """
    marker = root / PRUNE_MARKER
    current = time.time()
    if not force:
        try:
            if current - marker.stat().st_mtime < PRUNE_INTERVAL_SECONDS:
                return []
        except OSError:
            pass
    append(marker, b"")
    os.utime(marker, (current, current))
    deleted: list[Path] = []
    for path in expired_files(log_files(root), now().date(), keep_days):
        try:
            path.unlink()
        except OSError:
            continue
        deleted.append(path)
    return deleted


# Reading


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict):
                    yield line
    except OSError:
        return


def matches(line: Mapping[str, Any], name: str | None) -> bool:
    """True when the line's event is NAME or starts with `NAME.`."""
    if name is None:
        return True
    found = str(line.get("event", ""))
    return found == name or found.startswith(f"{name}.")


def tail(
    count: int,
    surface: str | None = None,
    name: str | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """The last COUNT events, oldest first, across every surface or one.

    NAME keeps one event, or a family by its prefix: `keeper` matches
    `keeper.request`.
    """
    if count <= 0:
        return []
    by_day: dict[date, list[Path]] = {}
    for day, found, path in log_files(root):
        if surface is None or found == surface:
            by_day.setdefault(day, []).append(path)
    lines: list[dict[str, Any]] = []
    for day in sorted(by_day, reverse=True):
        batch = [
            line for path in by_day[day] for line in _lines(path) if matches(line, name)
        ]
        lines = batch + lines
        if len(lines) >= count:
            break
    lines.sort(key=lambda line: (str(line.get("ts", "")), _number(line.get("seq"))))
    return lines[-count:]


def _number(value: Any) -> int:
    return value if isinstance(value, int) else 0


def render(line: Mapping[str, Any]) -> str:
    """One event as a line of text: time, surface, level, event, then its
    own fields as `key=value`."""
    head = " ".join(
        str(line.get(key, "?")) for key in ("ts", "surface", "level", "event")
    )
    rest = " ".join(
        f"{key}={json.dumps(value, separators=(',', ':'), default=str)}"
        if not isinstance(value, (str, int, float)) or isinstance(value, bool)
        else f"{key}={value}"
        for key, value in line.items()
        if key not in COMMON
    )
    return f"{head} {rest}".rstrip()
