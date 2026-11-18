"""Read-only update support: release tags, the latest release, install kind,
and the cached check result.

Nothing here installs anything. Release tags are `vMAJOR.MINOR.PATCH`;
pre-release tags such as `v0.2.0-rc.1` never count as the latest version.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

REPO = "SaiDotFun/open-sai"
API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
GIT_URL = f"https://github.com/{REPO}"
CONNECT_TIMEOUT = 3.0
TOTAL_TIMEOUT = 5.0
CACHE_SCHEMA = 1
# GitHub's API limits unauthenticated clients; the web redirect is not metered.
FALLBACK_STATUSES = frozenset({403, 429})

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


class UpdateError(Exception):
    """The latest release could not be determined."""


def parse_tag(tag: str) -> Version | None:
    """Return the version of a release tag, or None for anything else,
    including pre-release tags."""
    match = _TAG.match(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def parse_version(text: str) -> Version | None:
    """Return the version of a plain `X.Y.Z` package version, or None."""
    return parse_tag(f"v{text}")


def is_newer(latest: str, installed: str) -> bool:
    """True when release tag LATEST is newer than package version INSTALLED.

    An unparseable side never counts as newer, so a local or pre-release
    build is never told to move.
    """
    new, old = parse_tag(latest), parse_version(installed)
    return new is not None and old is not None and new > old


# Latest release


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sai-cli",
    }


def _checked(tag: Any) -> str:
    if not isinstance(tag, str) or parse_tag(tag) is None:
        raise UpdateError(f"latest release tag {tag!r} is not vMAJOR.MINOR.PATCH")
    return tag


# The API answer is a few kilobytes; refuse anything far larger.
MAX_BODY = 1 << 20
TIMED_OUT = "timed out checking the latest release"


def _timeout(deadline: float, connect: float) -> httpx.Timeout:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UpdateError(TIMED_OUT)
    return httpx.Timeout(remaining, connect=min(connect, remaining))


def _read_body(response: httpx.Response, deadline: float) -> bytes:
    """Read the body, giving up at DEADLINE even if bytes keep arriving."""
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if time.monotonic() > deadline:
            raise UpdateError(TIMED_OUT)
        if len(body) > MAX_BODY:
            raise UpdateError("GitHub API answer is too large")
    return bytes(body)


def _from_api(
    client: httpx.Client, url: str, deadline: float, connect: float
) -> str | None:
    """Return the tag from the API, or None when the fallback should run."""
    with client.stream("GET", url, timeout=_timeout(deadline, connect)) as response:
        status = response.status_code
        if status in FALLBACK_STATUSES or status >= 500:
            return None
        if status == 404:
            raise UpdateError("no release is published yet")
        if status != 200:
            raise UpdateError(f"GitHub API answered HTTP {status}")
        body = _read_body(response, deadline)
    try:
        return _checked(json.loads(body).get("tag_name"))
    except (ValueError, AttributeError) as exc:
        raise UpdateError("GitHub API answered with malformed JSON") from exc


def _from_redirect(
    client: httpx.Client, url: str, deadline: float, connect: float
) -> str:
    """Read the tag from the Location of the web /releases/latest redirect."""
    response = client.head(url, timeout=_timeout(deadline, connect))
    location = response.headers.get("location")
    if not response.is_redirect or not location:
        raise UpdateError(
            f"release page answered HTTP {response.status_code} without a redirect"
        )
    path = urlsplit(location).path.rstrip("/")
    prefix, _, tag = path.rpartition("/")
    if not prefix.endswith("/releases/tag"):
        raise UpdateError("release page redirected away from a release tag")
    return _checked(unquote(tag))


def _fetch(api: str, web: str, deadline: float, connect: float) -> str:
    try:
        with httpx.Client(headers=_headers(), follow_redirects=False) as client:
            tag = _from_api(client, api, deadline, connect)
            if tag is not None:
                return tag
            return _from_redirect(client, web, deadline, connect)
    except httpx.TimeoutException as exc:
        raise UpdateError(TIMED_OUT) from exc
    except httpx.HTTPError as exc:
        raise UpdateError(f"could not reach GitHub: {type(exc).__name__}") from exc


def fetch_latest(
    api_base: str = API_BASE,
    web_base: str = WEB_BASE,
    repo: str = REPO,
    connect_timeout: float = CONNECT_TIMEOUT,
    total_timeout: float = TOTAL_TIMEOUT,
) -> str:
    """Return the latest release tag of REPO.

    Asks the API first and falls back to the web redirect on 403, 429, or a
    5xx. Both requests share one TOTAL_TIMEOUT wall-clock budget. HTTPX
    timeouts apply per network operation, so a server that sends a byte at a
    time could outlast them; the lookup runs in a daemon thread that the
    caller stops waiting for at the deadline.
    """
    deadline = time.monotonic() + total_timeout
    api = f"{api_base.rstrip('/')}/repos/{repo}/releases/latest"
    web = f"{web_base.rstrip('/')}/{repo}/releases/latest"
    outcome: dict[str, str | Exception] = {}

    def work() -> None:
        try:
            outcome["tag"] = _fetch(api, web, deadline, connect_timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="sai-update-check", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        raise UpdateError(TIMED_OUT)
    error = outcome.get("error")
    if isinstance(error, Exception):
        raise error
    tag = outcome.get("tag")
    assert isinstance(tag, str)
    return tag


# Install kind

CHECKOUT = "checkout"
UV_TOOL_GIT = "uv-tool-git"
UV_TOOL_OTHER = "uv-tool-other"
UVX = "uvx"
OTHER = "other"


@dataclass(frozen=True)
class Install:
    """How this copy of sai-cli was installed.

    Only UV_TOOL_GIT, a `uv tool install` from this repository on GitHub,
    can be updated in place by a later release of `sai update`.
    """

    kind: str
    prefix: str
    url: str | None = None
    commit: str | None = None
    requested: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _direct_url(prefix: Path) -> dict[str, Any] | None:
    """Read direct_url.json of the sai-cli dist-info under PREFIX."""
    patterns = (
        "lib/python*/site-packages/sai_cli-*.dist-info/direct_url.json",
        "Lib/site-packages/sai_cli-*.dist-info/direct_url.json",
    )
    for pattern in patterns:
        for path in sorted(prefix.glob(pattern)):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
            return data if isinstance(data, dict) else None
    return None


def _is_this_repo(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix(".git").lower()
    return parts.hostname == "github.com" and path == f"/{REPO.lower()}"


def _public(url: str | None) -> str | None:
    """Drop user info, query, and fragment, which can hold credentials."""
    if not url:
        return None
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}" if parts.scheme else url


def install_kind(prefix: Path | None = None) -> Install:
    """Classify the environment at PREFIX (default: sys.prefix).

    - checkout: an editable install, as `uv sync` makes in a clone.
    - uv-tool-git: `uv tool install` from this repository on GitHub.
    - uv-tool-other: `uv tool install` from a directory, fork, or index.
    - uvx: a cached `uvx` environment.
    - other: anything else, such as pip.
    """
    root = Path(sys.prefix if prefix is None else prefix)
    direct = _direct_url(root) or {}
    url = direct.get("url") if isinstance(direct.get("url"), str) else None
    vcs = direct.get("vcs_info") if isinstance(direct.get("vcs_info"), dict) else {}
    directory = direct.get("dir_info")
    commit = vcs.get("commit_id") if vcs else None
    requested = vcs.get("requested_revision") if vcs else None

    def made(kind: str) -> Install:
        return Install(kind, str(root), _public(url), commit, requested)

    if isinstance(directory, dict) and directory.get("editable"):
        return made(CHECKOUT)
    if (root / "uv-receipt.toml").is_file():
        is_git = vcs.get("vcs") == "git" if vcs else False
        if is_git and url and _is_this_repo(url.removeprefix("git+")):
            return made(UV_TOOL_GIT)
        return made(UV_TOOL_OTHER)
    if root.parent.name == "archive-v0":
        return made(UVX)
    return made(OTHER)


def reinstall_command(tag: str | None = None) -> str:
    """The command that installs TAG, or main, from this repository."""
    ref = f"@{tag}" if tag else ""
    return f"uv tool install --reinstall git+{GIT_URL}{ref}"


@dataclass(frozen=True)
class Guidance:
    """How to move INSTALL to a newer release, without replacing a fork or
    local build with the upstream package."""

    note: str
    command: str | None = None


def _source(url: str | None) -> str | None:
    """The uv source that reinstalls from URL: a path or a git URL."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme == "file":
        return unquote(parts.path)
    return url if url.startswith("git+") else f"git+{url}"


def guidance(install: Install, tag: str) -> Guidance:
    """Say how to update INSTALL to release TAG.

    Only an install from this repository gets the upstream command; any
    other source is the user's to update.
    """
    if install.kind == UV_TOOL_GIT:
        return Guidance("Reinstall the release with uv.", reinstall_command(tag))
    if install.kind == CHECKOUT:
        return Guidance("This is a source checkout; pull, then run `uv sync`.")
    if install.kind == UVX:
        return Guidance(
            "uvx runs a cached copy; name the release to run it.",
            f"uvx --from git+{GIT_URL}@{tag} sai",
        )
    source = _source(install.url)
    if install.kind == UV_TOOL_OTHER and source:
        return Guidance(
            f"This sai was installed from {install.url}, not {REPO}. Update"
            " that source, then reinstall from it.",
            f"uv tool install --reinstall {source}",
        )
    return Guidance(
        "This sai was not installed from this repository's releases. Update it"
        " the way it was installed."
    )


# Cache


def sai_home() -> Path:
    return Path(os.environ.get("SAI_HOME", Path.home() / ".sai"))


def cache_path() -> Path:
    return sai_home() / "update.json"


@dataclass(frozen=True)
class Cache:
    """The last successful check: the latest tag and when it was seen."""

    latest: str
    checked_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": CACHE_SCHEMA,
            "latest": self.latest,
            "checkedAt": self.checked_at,
        }


def read_cache(path: Path | None = None) -> Cache | None:
    """Return the cached check, or None when it is missing or unreadable.

    A corrupt file or another schema counts as missing.
    """
    try:
        data = json.loads((path or cache_path()).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
        return None
    latest, checked_at = data.get("latest"), data.get("checkedAt")
    if not isinstance(latest, str) or parse_tag(latest) is None:
        return None
    if not isinstance(checked_at, str):
        return None
    return Cache(latest, checked_at)


def write_cache(cache: Cache, path: Path | None = None) -> None:
    """Replace the cache file atomically."""
    target = path or cache_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(cache.to_json(), handle)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def latest_version(path: Path | None = None) -> str | None:
    """The cached latest version without the `v`, read with no network."""
    cache = read_cache(path)
    return cache.latest.removeprefix("v") if cache else None
