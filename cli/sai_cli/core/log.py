"""Structured logs: one JSON object per line under $SAI_HOME/logs.

Each surface writes its own file per UTC day, `<surface>-YYYY-MM-DD.jsonl`,
where the surface is `cli`, `mcp`, or `update`. The directory has mode 0700
and the files 0600.

Every line carries `ts`, `level`, `event`, `surface`, `pid`, `version`,
`runId` (one per process), and `seq` (the event's number within the run,
which orders events that share a millisecond), then the fields bound with
`bind`, then the event's own fields.

`event` does no I/O and never blocks: it stamps the line and puts it on a
bounded queue. One daemon thread drains the queue and does everything that
can wait on a disk: reading the configuration, redacting, opening, writing,
and deleting old files. When the queue is full the event is dropped and
counted, and a later `log.dropped` line reports the count. The process waits
a short time at exit for the queue to drain. So a slow or stuck filesystem
can cost log lines, never an MCP reply or the time between reserving an
intent and submitting it.

Callers pass only values that are safe to store: never the session token, a
signature, or clipboard text. The writer still treats every string as
untrusted: it reduces URLs to their origin, and removes stored session
tokens, anything shaped like a JWT, and long hex strings such as signatures.
Text from outside sai, wrapped in `untrusted`, is cut shorter and loses hex
strings of 64 digits or more.

The writer opens the logs directory without following a symlink, checks that
this user owns it, and opens, checks, and deletes files relative to that
descriptor. It appends only to a regular file with one link that this user
owns, after removing any group and other access from it. Anything else is left alone and the line is
lost.

Logging never writes to stdout or stderr and never raises. Commands that
promise to change nothing, such as `sai doctor --no-fix`, run inside
`silenced` and write no lines.

`logs.enabled` (SAI_LOG) turns logging off; `logs.keep_days`
(SAI_LOG_KEEP_DAYS) sets how many days of files are kept. A logs setting or
config file that cannot be read also turns logging off, since it cannot say
what the person chose. Old files are deleted once per UTC day.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import re
import stat
import threading
import time
import uuid
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Self
from urllib.parse import urlsplit

DIR_NAME = "logs"
SURFACES = ("cli", "mcp", "update")
LEVELS = ("info", "warn", "error")
DIR_MODE, FILE_MODE = 0o700, 0o600
DEFAULT_KEEP_DAYS = 14
PRUNE_MARKER = ".pruned"
# Longest string kept in a field, longest text from outside sai, and
# longest line written.
MAX_TEXT = 2000
MAX_UNTRUSTED = 300
MAX_LINE = 16 * 1024
# Events waiting for the writer. Past this, events are dropped and counted.
QUEUE_SIZE = 2048
# How long the process waits at exit for the queue to drain.
EXIT_FLUSH_SECONDS = 2.0
# How long `event` skips a home whose logging is off or failing before it
# lets the writer look again.
RECHECK_SECONDS = 5.0
TAIL_CHUNK = 64 * 1024
REDACTED = "[redacted session token]"
REDACTED_HEX = "[redacted hex]"
REDACTED_URL = "[redacted url]"
COMMON = ("ts", "level", "event", "surface", "pid", "version", "runId", "seq")

RUN_ID = uuid.uuid4().hex
_seq = itertools.count(1)
_FILE = re.compile(
    rf"^(?P<surface>{'|'.join(SURFACES)})-(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\.jsonl$"
)
# Three base64url segments, the first a JSON object: a JWT such as the
# session token.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>\\]+")
# A 65-byte signature is 130 hex digits. A transaction hash or a preview
# digest is 64, and stays unless the text is `untrusted`.
_LONG_HEX = re.compile(r"(?:0x)?[0-9A-Fa-f]{100,}")
_HEX = re.compile(r"(?:0x)?[0-9A-Fa-f]{64,}")
_TRUE = ("true", "1", "yes", "on")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Open files relative to a directory descriptor where the platform can.
_DIR_FD = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd

_context: ContextVar[Mapping[str, Any]] = ContextVar(
    "sai_log_context", default=MappingProxyType({})
)
# How many `silenced` blocks are open, in any thread.
_silenced = 0


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


def now() -> datetime:
    return datetime.now(UTC)


def stamp(instant: datetime) -> str:
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Configuration


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def _off(raw: str | None) -> bool:
    """True when the SAI_LOG value RAW turns logging off, or is not a
    boolean at all. Reads nothing."""
    text = (raw or "").strip().lower()
    return bool(text) and text not in _TRUE


_config_cache: dict[tuple[Any, ...], tuple[bool, int]] = {}


def resolve_config(
    root: Path, raw_enabled: str | None, raw_keep: str | None
) -> tuple[bool, int]:
    """Return (`logs.enabled`, `logs.keep_days`) for the home ROOT from the
    two environment values and its config file.

    Each key resolves on its own, environment first. Any logs value that is
    invalid, or a config file that cannot be read or parsed, turns logging
    off: a typo in one key must not undo an opt-out in the other. The answer
    is cached until the file or the values change.
    """
    from . import settings

    path = root / settings.FILE_NAME
    key = (str(path), _stat_key(path), raw_enabled, raw_keep)
    cached = _config_cache.get(key)
    if cached is not None:
        return cached
    try:
        file, _warnings = settings.read_file(path)
        found: list[Any] = []
        for name, raw in (("logs.enabled", raw_enabled), ("logs.keep_days", raw_keep)):
            if raw is not None and raw.strip():
                found.append(settings.parse_value(name, raw))
            else:
                found.append(file.get(name, settings.BY_KEY[name].default))
        value = (bool(found[0]), int(found[1]))
    except Exception:  # noqa: BLE001 - unreadable settings mean no logging
        value = (False, DEFAULT_KEEP_DAYS)
    if len(_config_cache) > 64:
        _config_cache.clear()
    _config_cache[key] = value
    return value


def config() -> tuple[bool, int]:
    """Return (`logs.enabled`, `logs.keep_days`) for this process's
    environment and config file."""
    return resolve_config(
        home(), os.environ.get("SAI_LOG"), os.environ.get("SAI_LOG_KEEP_DAYS")
    )


def enabled() -> bool:
    return config()[0]


# Redaction


class Untrusted(str):
    """Text from outside sai, such as an error message Keeper returned."""

    __slots__ = ()


class Endpoint(str):
    """A whole endpoint URL, logged as its origin only."""

    __slots__ = ()


def untrusted(value: Any) -> Untrusted | None:
    """Mark VALUE as text from outside sai: it is cut to MAX_UNTRUSTED
    characters and loses hex strings of 64 digits or more."""
    return None if value is None else Untrusted(value)


def endpoint(value: Any) -> Endpoint | None:
    """Mark VALUE as an endpoint URL: only its scheme, host, and port are
    logged, since the path and query can hold an API key. Parses nothing."""
    return None if value is None else Endpoint(value)


def origin(value: str) -> str:
    """The scheme, host, and port of the URL VALUE, or a placeholder when it
    does not parse. Never raises."""
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
        if not parts.scheme or not host:
            return REDACTED_URL
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        return f"{parts.scheme}://{host}" + ("" if port is None else f":{port}")
    except ValueError:
        return REDACTED_URL


_token_cache: tuple[Any, tuple[str, ...]] | None = None


def _tokens() -> tuple[str, ...]:
    """Every stored session token. A successful read is cached until the
    session file changes; a failed read is retried on the next call."""
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
        _token_cache = None
        return ()
    _token_cache = (key, tokens)
    return tokens


def redact(text: str) -> str:
    """Remove every connected account's session token from TEXT."""
    for token in _tokens():
        text = text.replace(token, REDACTED)
    return text


def _cut(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def scrub(value: str, tokens: tuple[str, ...] | None = None) -> str:
    """VALUE with secrets removed and its length capped. TOKENS are the
    stored session tokens, read when not given."""
    if isinstance(value, Endpoint):
        return origin(value)
    text = str(value)
    for token in _tokens() if tokens is None else tokens:
        text = text.replace(token, REDACTED)
    text = _JWT.sub(REDACTED, text)
    text = _URL.sub(lambda match: origin(match.group()), text)
    if isinstance(value, Untrusted):
        return _cut(_HEX.sub(REDACTED_HEX, text), MAX_UNTRUSTED)
    return _cut(_LONG_HEX.sub(REDACTED_HEX, text), MAX_TEXT)


def clean(value: Any, tokens: tuple[str, ...] | None = None) -> Any:
    """Return VALUE as JSON-ready data with every string scrubbed."""
    found = _tokens() if tokens is None else tokens
    if isinstance(value, str):
        return scrub(value, found)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            scrub(str(key), found): clean(item, found) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean(item, found) for item in value]
    return scrub(str(value), found)


def _snapshot(value: Any, depth: int = 0) -> Any:
    """Copy VALUE so later changes by the caller do not reach the writer.
    Strings keep their `Untrusted` or `Endpoint` type."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if depth > 6:
        return type(value).__name__
    if isinstance(value, Mapping):
        return {str(k): _snapshot(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_snapshot(item, depth + 1) for item in value]
    return str(value)


# Context


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


# The logs directory


class LogDir:
    """The logs directory, opened without following a symlink.

    Files are opened, checked, and deleted relative to the directory's
    descriptor, so replacing the directory with a symlink after it was
    opened redirects nothing. Where the platform has no directory
    descriptors, paths are used and symlinks are refused by name.
    """

    def __init__(self, root: Path, fd: int | None) -> None:
        self.root = root
        self.fd = fd

    @classmethod
    def open(cls, root: Path, *, create: bool = False) -> LogDir | None:
        """Open ROOT, or return None when it is missing, a symlink, not a
        directory, or owned by someone else. With CREATE, make it first, and
        remove group and other access from a directory this user owns."""
        try:
            if create and not root.is_symlink() and not root.exists():
                root.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
                with suppress(FileExistsError):
                    root.mkdir(mode=DIR_MODE)
            if not _DIR_FD:
                if root.is_symlink() or not root.is_dir():
                    return None
                return cls(root, None)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                os.close(fd)
                return None
            mode = stat.S_IMODE(info.st_mode)
            if create and mode & 0o077:
                os.fchmod(fd, mode & ~0o077)
        except OSError:
            os.close(fd)
            return None
        return cls(root, fd)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _target(self, name: str) -> tuple[str | Path, dict[str, Any]]:
        if self.fd is None:
            return self.root / name, {}
        return name, {"dir_fd": self.fd}

    def names(self) -> list[str]:
        try:
            return sorted(os.listdir(self.root if self.fd is None else self.fd))
        except OSError:
            return []

    def _open_file(self, name: str, flags: int) -> int:
        """Open NAME and return its descriptor, or raise OSError unless it
        is a regular file with one link that this user owns. Never waits:
        a FIFO or device in its place fails instead of blocking."""
        target, extra = self._target(name)
        if self.fd is None and Path(target).is_symlink():
            raise OSError("log file is a symlink")
        fd = os.open(target, flags | _NOFOLLOW | _NONBLOCK, FILE_MODE, **extra)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError("log file is not a private regular file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise OSError("log file belongs to another user")
        except OSError:
            os.close(fd)
            raise
        return fd

    def append(self, name: str, data: bytes) -> None:
        """Append DATA to NAME with one write, creating it at mode 0600 and
        removing group and other access from an existing file first."""
        fd = self._open_file(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode & 0o077 and hasattr(os, "fchmod"):
                os.fchmod(fd, mode & ~0o077)
            os.write(fd, data)
        finally:
            os.close(fd)

    def read_fd(self, name: str) -> int:
        return self._open_file(name, os.O_RDONLY)

    def modified(self, name: str) -> float | None:
        target, extra = self._target(name)
        try:
            return os.stat(target, follow_symlinks=False, **extra).st_mtime
        except OSError:
            return None

    def touch(self, name: str) -> None:
        """Create NAME if missing and set its modification time to now."""
        fd = self._open_file(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            if os.utime in os.supports_fd:
                os.utime(fd)
            else:
                os.utime(self.root / name)
        finally:
            os.close(fd)

    def is_regular(self, name: str) -> bool:
        target, extra = self._target(name)
        try:
            info = os.stat(target, follow_symlinks=False, **extra)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def unlink(self, name: str) -> bool:
        target, extra = self._target(name)
        try:
            os.unlink(target, **extra)
        except OSError:
            return False
        return True

    def files(self) -> list[tuple[date, str, str]]:
        """Sai's log files as (day, surface, name), oldest first: regular
        files named exactly `<surface>-YYYY-MM-DD.jsonl`."""
        found: list[tuple[date, str, str]] = []
        for name in self.names():
            parsed = parse_name(name)
            if parsed is not None and self.is_regular(name):
                found.append((*parsed, name))
        return sorted(found)


def parse_name(name: str) -> tuple[date, str] | None:
    """The day and surface in a log file NAME, or None for any other name."""
    match = _FILE.match(name)
    if match is None:
        return None
    try:
        return date.fromisoformat(match["date"]), match["surface"]
    except ValueError:
        return None


def file_name(surface: str, day: date) -> str:
    return f"{surface}-{day.isoformat()}.jsonl"


def file_path(surface: str, day: date, root: Path | None = None) -> Path:
    return (root or directory()) / file_name(surface, day)


def log_files(root: Path | None = None) -> list[tuple[date, str, Path]]:
    """Every log file under ROOT as (day, surface, path), oldest first.
    Empty when ROOT is missing, a symlink, or not a directory."""
    base = root or directory()
    opened = LogDir.open(base)
    if opened is None:
        return []
    with opened:
        return [(day, surface, base / name) for day, surface, name in opened.files()]


# Retention


def expired_files(
    files: list[tuple[date, str, Any]], today: date, keep_days: int
) -> list[Any]:
    """The files older than KEEP_DAYS days: today and the KEEP_DAYS - 1
    days before it are kept."""
    oldest = today - timedelta(days=max(keep_days, 1) - 1)
    return [path for day, _surface, path in files if day < oldest]


def prune(opened: LogDir, keep_days: int, today: date) -> list[str]:
    """Delete log files older than KEEP_DAYS days, once per UTC day.

    The marker file's modification time records the last run across
    processes: a marker from TODAY or later means some process already
    pruned. Returns the names deleted.
    """
    last = opened.modified(PRUNE_MARKER)
    if last is not None and datetime.fromtimestamp(last, UTC).date() >= today:
        return []
    opened.touch(PRUNE_MARKER)
    expired = expired_files(opened.files(), today, keep_days)
    return [name for name in expired if opened.unlink(name)]


# Writing


class _Item:
    """One event waiting for the writer, with the home and the logging
    values of the environment it was written under."""

    __slots__ = ("keep", "line", "on", "root")

    def __init__(
        self, root: str, on: str | None, keep: str | None, line: dict[str, Any]
    ) -> None:
        self.root = root
        self.on = on
        self.keep = keep
        self.line = line


_queue: queue.Queue[_Item] = queue.Queue(maxsize=QUEUE_SIZE)
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
# Held by the writer while it handles one event.
_io_lock = threading.Lock()
_dropped = 0
# Homes whose logging is off or failing, with when the writer last looked.
_skip: dict[tuple[str, str | None, str | None], float] = {}
# The UTC day each home was last checked for old files.
_pruned: dict[str, date] = {}


def encode(line: Mapping[str, Any]) -> bytes:
    """One JSON line. A line over MAX_LINE keeps its common fields and says
    which fields it dropped."""
    data = json.dumps(line, separators=(",", ":"), default=str)
    if len(data.encode()) > MAX_LINE:
        kept = {key: line[key] for key in COMMON if key in line}
        kept["truncated"] = sorted(key for key in line if key not in COMMON)
        data = json.dumps(kept, separators=(",", ":"), default=str)
    return data.encode() + b"\n"


def _write(item: _Item) -> None:
    """Redact and append one event. Runs on the writer thread."""
    global _dropped
    key = (item.root, item.on, item.keep)
    root = Path(item.root)
    on, keep_days = resolve_config(root, item.on, item.keep)
    if not on:
        _skip[key] = time.monotonic()
        return
    opened = LogDir.open(root / DIR_NAME, create=True)
    if opened is None:
        _skip[key] = time.monotonic()
        return
    with opened:
        _skip.pop(key, None)
        line = clean(item.line)
        today = now().date()
        data = encode(line)
        if _dropped:
            lost, _dropped = _dropped, 0
            note = {k: line[k] for k in COMMON}
            note.update(level="warn", event="log.dropped", count=lost)
            data += encode(note)
        # A file that cannot be appended to loses this line only; the other
        # surfaces' files may be fine.
        with suppress(OSError):
            opened.append(file_name(line["surface"], today), data)
        if _pruned.get(item.root) != today:
            _pruned[item.root] = today
            with suppress(OSError):
                prune(opened, keep_days, today)


def _drain() -> None:
    while True:
        item = _queue.get()
        try:
            with _io_lock:
                _write(item)
        except Exception:  # noqa: BLE001, S110 - a log line must not break a command
            pass
        finally:
            _queue.task_done()


def _start() -> None:
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_drain, name="sai-log", daemon=True)
            _thread.start()


def event(name: str, level: str = "info", /, **fields: Any) -> None:
    """Queue one event for the writer. Does no I/O, never blocks, and never
    raises. With SAI_LOG off it returns before looking at FIELDS."""
    global _dropped
    try:
        if _silenced:
            return
        raw_on = os.environ.get("SAI_LOG")
        if _off(raw_on):
            return
        key = (str(home()), raw_on, os.environ.get("SAI_LOG_KEEP_DAYS"))
        skipped = _skip.get(key)
        if skipped is not None and time.monotonic() - skipped < RECHECK_SECONDS:
            return
        context = dict(_context.get())
        surface = context.pop("surface", "cli")
        extra = {k: v for k, v in {**context, **fields}.items() if v is not None}
        line = {
            "ts": stamp(now()),
            "level": level if level in LEVELS else "info",
            "event": str(name),
            "surface": surface if surface in SURFACES else "cli",
            "pid": os.getpid(),
            "version": VERSION,
            "runId": RUN_ID,
            "seq": next(_seq),
            **_snapshot(extra),
        }
        try:
            _queue.put_nowait(_Item(*key, line))
        except queue.Full:
            _dropped += 1
            return
        if _thread is None or not _thread.is_alive():
            _start()
    except Exception:  # noqa: BLE001, S110 - a log line must not break a command
        pass


def flush(timeout: float = 5.0) -> bool:
    """Wait up to TIMEOUT seconds for queued events to be written. Returns
    whether the queue drained."""
    end = time.monotonic() + timeout
    with _queue.all_tasks_done:
        while _queue.unfinished_tasks:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            _queue.all_tasks_done.wait(remaining)
    return True


atexit.register(flush, EXIT_FLUSH_SECONDS)


def reset() -> None:
    """Write what is queued, then forget cached state, so the next event
    rereads the configuration and retries a directory that failed. For
    tests."""
    global _token_cache, _silenced, _dropped
    flush()
    _silenced = 0
    _dropped = 0
    _token_cache = None
    _config_cache.clear()
    _skip.clear()
    _pruned.clear()


# Reading


def _last_lines(fd: int, chunk: int) -> Iterator[bytes]:
    """The lines of the file at FD, last first, read backwards CHUNK bytes
    at a time."""
    position = os.fstat(fd).st_size
    rest = b""
    while position > 0:
        size = min(chunk, position)
        position -= size
        os.lseek(fd, position, os.SEEK_SET)
        data = os.read(fd, size) + rest
        pieces = data.split(b"\n")
        rest = pieces[0]
        for piece in reversed(pieces[1:]):
            if piece:
                yield piece
    if rest:
        yield rest


def matches(line: Mapping[str, Any], name: str | None) -> bool:
    """True when the line's event is NAME or starts with `NAME.`."""
    if name is None:
        return True
    found = str(line.get("event", ""))
    return found == name or found.startswith(f"{name}.")


def _file_tail(
    opened: LogDir, file: str, count: int, name: str | None, chunk: int
) -> list[dict[str, Any]]:
    """The last COUNT matching events in FILE, newest first."""
    found: list[dict[str, Any]] = []
    try:
        fd = opened.read_fd(file)
    except OSError:
        return found
    try:
        for raw in _last_lines(fd, chunk):
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and matches(line, name):
                found.append(line)
                if len(found) >= count:
                    break
    except OSError:
        pass
    finally:
        os.close(fd)
    return found


def _number(value: Any) -> int:
    return value if isinstance(value, int) else 0


def tail(
    count: int,
    surface: str | None = None,
    name: str | None = None,
    root: Path | None = None,
    chunk: int = TAIL_CHUNK,
) -> list[dict[str, Any]]:
    """The last COUNT events, oldest first, across every surface or one.

    NAME keeps one event, or a family by its prefix: `keeper` matches
    `keeper.request`. Each file is read from its end, and no more than
    COUNT events are kept from any one file.
    """
    if count <= 0:
        return []
    flush(1.0)
    opened = LogDir.open(root or directory())
    if opened is None:
        return []
    with opened:
        by_day: dict[date, list[str]] = {}
        for day, found, file in opened.files():
            if surface is None or found == surface:
                by_day.setdefault(day, []).append(file)
        lines: list[dict[str, Any]] = []
        for day in sorted(by_day, reverse=True):
            for file in by_day[day]:
                lines += _file_tail(opened, file, count, name, chunk)
            if len(lines) >= count:
                break
    lines.sort(key=lambda line: (str(line.get("ts", "")), _number(line.get("seq"))))
    return lines[-count:]


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
