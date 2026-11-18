"""Read-only update support: release tags, the latest release, install kind,
and the cached check result.

Nothing here installs anything; `updater` does. Release tags are `sai-cli/vMAJOR.MINOR.PATCH`,
so other components of the repository can release under their own prefixes.
Pre-release tags such as `sai-cli/v0.2.0-rc.1` never count as the latest
version.
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
from urllib.parse import quote, unquote, urlsplit

import httpx

REPO = "SaiDotFun/open-sai"
API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
GIT_URL = f"https://github.com/{REPO}"
TAG_PREFIX = "sai-cli/v"
# Installs the newest release; scripts/install.py in this repository.
INSTALL_COMMAND = (
    f"uv run https://raw.githubusercontent.com/{REPO}/main/scripts/install.py"
)
# The check runs in the background, so the budget is generous; it is still a
# hard wall-clock limit.
CONNECT_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
CACHE_SCHEMA = 1
# GitHub's API limits unauthenticated clients; the web redirect is not metered.
FALLBACK_STATUSES = frozenset({403, 429})
# Releases are listed newest first. Other components share the list, so read
# a few pages before deciding sai-cli has no release.
PAGE_SIZE = 30
MAX_PAGES = 4

_TAG = re.compile(rf"^{re.escape(TAG_PREFIX)}(\d+)\.(\d+)\.(\d+)$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


class UpdateError(Exception):
    """The latest release could not be determined."""


class NoRelease(UpdateError):
    """The repository has no sai-cli release yet."""


def parse_tag(tag: str) -> Version | None:
    """Return the version of a sai-cli release tag, or None for anything else,
    including pre-release tags and other components' tags."""
    match = _TAG.match(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def parse_version(text: str) -> Version | None:
    """Return the version of a plain `X.Y.Z` package version, or None."""
    match = _VERSION.match(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def tag_for(version: str) -> str:
    """The release tag of package version VERSION."""
    return f"{TAG_PREFIX}{version}"


def tag_version(tag: str) -> str:
    """The package version a release tag names: `sai-cli/v0.2.0` is `0.2.0`."""
    return tag.removeprefix(TAG_PREFIX)


def is_newer(latest: str, installed: str) -> bool:
    """True when release tag LATEST is newer than package version INSTALLED.

    An unparseable side never counts as newer, so a local or pre-release
    build is never told to move.
    """
    new, old = parse_tag(latest), parse_version(installed)
    return new is not None and old is not None and new > old


# Releases


@dataclass(frozen=True)
class Release:
    """A published sai-cli release and where its files download from."""

    tag: str
    wheel_url: str
    sums_url: str

    @property
    def version(self) -> str:
        return tag_version(self.tag)


def wheel_name(version: str) -> str:
    """The wheel file a release of VERSION attaches. sai-cli is pure Python,
    so every platform shares one wheel."""
    return f"sai_cli-{version}-py3-none-any.whl"


def release_for(tag: str, web_base: str = WEB_BASE, repo: str = REPO) -> Release:
    """Build the release of TAG with its download URLs.

    The URLs come from the tag alone, never from a URL an API answer names.
    """
    base = f"{web_base.rstrip('/')}/{repo}/releases/download/{quote(tag)}"
    return Release(tag, f"{base}/{wheel_name(tag_version(tag))}", f"{base}/SHA256SUMS")


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sai-cli",
    }


def _checked(tag: Any) -> str:
    if not isinstance(tag, str) or parse_tag(tag) is None:
        raise UpdateError(f"latest release tag {tag!r} is not {TAG_PREFIX}X.Y.Z")
    return tag


# A page of releases with long notes can reach a few hundred kilobytes;
# refuse anything far larger.
MAX_BODY = 4 << 20
TIMED_OUT = "timed out checking the latest release"
NO_RELEASE = "no sai-cli release is published yet"


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


def latest_tag(releases: list[Any]) -> str | None:
    """Return the highest sai-cli release tag in RELEASES, or None.

    Drafts, pre-releases, and tags of other components do not count.
    """
    best: tuple[Version, str] | None = None
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = item.get("tag_name")
        version = parse_tag(tag) if isinstance(tag, str) else None
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    return best[1] if best else None


def _page(
    client: httpx.Client, url: str, page: int, deadline: float, connect: float
) -> list[Any] | None:
    """Return one page of releases, or None when the fallback should run."""
    params = {"per_page": PAGE_SIZE, "page": page}
    timeout = _timeout(deadline, connect)
    with client.stream("GET", url, params=params, timeout=timeout) as response:
        status = response.status_code
        if status in FALLBACK_STATUSES or status >= 500:
            return None
        if status != 200:
            raise UpdateError(f"GitHub API answered HTTP {status}")
        body = _read_body(response, deadline)
    try:
        releases = json.loads(body)
    except ValueError as exc:
        raise UpdateError("GitHub API answered with malformed JSON") from exc
    if not isinstance(releases, list):
        raise UpdateError("GitHub API answered with malformed JSON")
    return releases


def _from_api(
    client: httpx.Client, url: str, deadline: float, connect: float
) -> str | None:
    """Return the highest sai-cli tag in the release list, or None when the
    fallback should run.

    The list is newest first, so the pages stop at the first one that holds a
    sai-cli release; an older page cannot hold a higher version unless a
    lower version was released after it.
    """
    for page in range(1, MAX_PAGES + 1):
        releases = _page(client, url, page, deadline, connect)
        if releases is None:
            return None
        tag = latest_tag(releases)
        if tag is not None:
            return tag
        if len(releases) < PAGE_SIZE:
            break
    raise NoRelease(NO_RELEASE)


def _from_redirect(
    client: httpx.Client, url: str, deadline: float, connect: float
) -> str:
    """Read the tag from the Location of the web /releases/latest redirect.

    That page follows GitHub's single latest release. When another component
    released last, the redirect names its tag and this fails; the next check
    that reaches the API finds the sai-cli release.
    """
    response = client.head(url, timeout=_timeout(deadline, connect))
    location = response.headers.get("location")
    if not response.is_redirect or not location:
        raise UpdateError(
            f"release page answered HTTP {response.status_code} without a redirect"
        )
    path = unquote(urlsplit(location).path).rstrip("/")
    if path.endswith("/releases"):
        raise NoRelease(NO_RELEASE)
    _, marker, tag = path.partition("/releases/tag/")
    if not marker:
        raise UpdateError("release page redirected away from a release tag")
    return _checked(tag)


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
) -> Release:
    """Return the latest sai-cli release of REPO.

    Lists the releases through the API and takes the highest `sai-cli/v*` tag
    that is not a pre-release. GitHub's own "latest" is one release for the
    whole repository, so it is only the fallback, on 403, 429, or a 5xx.
    Raises NoRelease when sai-cli has no release yet.

    All requests share one TOTAL_TIMEOUT wall-clock budget. HTTPX timeouts
    apply per network operation, so a server that sends a byte at a time
    could outlast them; the lookup runs in a daemon thread that the caller
    stops waiting for at the deadline.
    """
    deadline = time.monotonic() + total_timeout
    api = f"{api_base.rstrip('/')}/repos/{repo}/releases"
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
    return release_for(tag, web_base, repo)


# Install kind

CHECKOUT = "checkout"
UV_TOOL_RELEASE = "uv-tool-release"
UV_TOOL_GIT = "uv-tool-git"
UV_TOOL_OTHER = "uv-tool-other"
UVX = "uvx"
OTHER = "other"


@dataclass(frozen=True)
class Install:
    """How this copy of sai-cli was installed.

    `sai update` only changes an official install: a `uv tool install` of a
    release wheel (UV_TOOL_RELEASE) or of this repository's Git source
    (UV_TOOL_GIT).
    """

    kind: str
    prefix: str
    url: str | None = None
    commit: str | None = None
    requested: str | None = None

    @property
    def official(self) -> bool:
        return self.kind in (UV_TOOL_RELEASE, UV_TOOL_GIT)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _dist_info(prefix: Path) -> Path | None:
    """The sai-cli dist-info directory of the environment at PREFIX."""
    patterns = (
        "lib/python*/site-packages/sai_cli-*.dist-info",
        "Lib/site-packages/sai_cli-*.dist-info",
    )
    for pattern in patterns:
        for path in sorted(prefix.glob(pattern)):
            return path
    return None


def _direct_url(prefix: Path) -> dict[str, Any] | None:
    """Read direct_url.json of the sai-cli dist-info under PREFIX."""
    info = _dist_info(prefix)
    if info is None:
        return None
    try:
        data = json.loads((info / "direct_url.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def installed_version(prefix: Path | None = None) -> str | None:
    """The sai-cli version installed at PREFIX (default: sys.prefix), read
    from disk so it is current after a reinstall."""
    info = _dist_info(Path(sys.prefix if prefix is None else prefix))
    if info is None:
        return None
    try:
        lines = (info / "METADATA").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            break
        name, _, value = line.partition(":")
        if name.lower() == "version":
            return value.strip()
    return None


def _is_this_repo(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix(".git").lower()
    return parts.hostname == "github.com" and path == f"/{REPO.lower()}"


def _is_release_wheel(url: str, web_base: str = WEB_BASE) -> bool:
    """True for a wheel under this repository's sai-cli release downloads."""
    parts, base = urlsplit(url), urlsplit(web_base)
    prefix = f"{base.path.rstrip('/')}/{REPO}/releases/download/{TAG_PREFIX}"
    return (
        (parts.scheme, parts.netloc) == (base.scheme, base.netloc)
        and unquote(parts.path).lower().startswith(prefix.lower())
        and parts.path.endswith(".whl")
    )


_CACHED_WHEEL = re.compile(r"^sai_cli-\d+\.\d+\.\d+-py3-none-any\.whl$")


def _is_cached_wheel(url: str) -> bool:
    """True for a release wheel `sai update` verified and kept in its cache."""
    parts = urlsplit(url)
    if parts.scheme != "file":
        return False
    path = Path(unquote(parts.path))
    try:
        inside = path.parent.resolve() == wheel_cache().resolve()
    except OSError:
        return False
    return inside and _CACHED_WHEEL.match(path.name) is not None


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


def install_kind(prefix: Path | None = None, web_base: str = WEB_BASE) -> Install:
    """Classify the environment at PREFIX (default: sys.prefix).

    - checkout: an editable install, as `uv sync` makes in a clone.
    - uv-tool-release: `uv tool install` of a sai-cli release wheel, from
      GitHub or from the cache `sai update` installs out of.
    - uv-tool-git: `uv tool install` from this repository's Git source.
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
        from_release = bool(url) and (
            _is_release_wheel(url, web_base) or _is_cached_wheel(url)
        )
        if not vcs and from_release:
            return made(UV_TOOL_RELEASE)
        is_git = vcs.get("vcs") == "git" if vcs else False
        if is_git and url and _is_this_repo(url.removeprefix("git+")):
            return made(UV_TOOL_GIT)
        return made(UV_TOOL_OTHER)
    if root.parent.name == "archive-v0":
        return made(UVX)
    return made(OTHER)


@dataclass(frozen=True)
class Guidance:
    """How to move an install to a newer release."""

    note: str
    command: str | None = None


def guidance(install: Install) -> Guidance:
    """Say how to move INSTALL to the latest release.

    Only an official uv tool install is supported. A checkout updates with
    git; anything else is pointed at the supported install.
    """
    if install.kind == CHECKOUT:
        return Guidance("This is a source checkout; pull, then run `uv sync`.")
    if install.official:
        return Guidance("Run `sai update` to install it.")
    return Guidance(
        f"Only uv tool installs from {REPO} are supported. To switch to one, run:",
        INSTALL_COMMAND,
    )


# Cache


def sai_home() -> Path:
    return Path(os.environ.get("SAI_HOME", Path.home() / ".sai"))


def cache_path() -> Path:
    return sai_home() / "update.json"


def wheel_cache() -> Path:
    """Where `sai update` keeps the wheels it verified and installed."""
    return sai_home() / "cache" / "wheels"


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
    """The cached latest version as `X.Y.Z`, read with no network."""
    cache = read_cache(path)
    return tag_version(cache.latest) if cache else None
