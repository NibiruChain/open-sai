# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Install the newest sai-cli release with uv.

Finds the highest `sai-cli/vX.Y.Z` release of SaiDotFun/open-sai and runs
`uv tool install` on its wheel. `--source` builds that release from Git
instead. Before the first release exists, it installs from `main` and says so.

It needs only uv and the standard library, so it runs straight from GitHub:

    uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main/scripts/install.py

Usage: install.py [--source] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, unquote, urlsplit

# Keep these in step with cli/sai_cli/core/update.py; a test compares them.
REPO = "SaiDotFun/open-sai"
API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
TAG_PREFIX = "sai-cli/v"
PAGE_SIZE = 30
MAX_PAGES = 4
MAX_BODY = 4 << 20
REQUEST_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
# GitHub's API limits unauthenticated clients; the web redirect is not metered.
FALLBACK_STATUSES = frozenset({403, 429})

TAG = re.compile(rf"^{re.escape(TAG_PREFIX)}(\d+)\.(\d+)\.(\d+)$")


class LookupFailed(Exception):
    """The latest release could not be determined."""


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    """Return the version of a sai-cli release tag, or None for anything else,
    including pre-release tags and other components' tags."""
    match = TAG.match(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def latest_tag(releases: list[Any]) -> str | None:
    """Return the highest sai-cli release tag in RELEASES, or None."""
    best: tuple[tuple[int, int, int], str] | None = None
    for item in releases:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        tag = item.get("tag_name")
        version = parse_tag(tag) if isinstance(tag, str) else None
        if version is not None and (best is None or version > best[0]):
            best = (version, tag)
    return best[1] if best else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def request(
    url: str, method: str, deadline: float
) -> tuple[int, dict[str, str], bytes]:
    """Return the status, headers, and body of URL without following
    redirects."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LookupFailed("timed out looking up the latest release")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sai-cli"}
    opener = urllib.request.build_opener(NoRedirect)
    call = urllib.request.Request(url, method=method, headers=headers)
    try:
        with opener.open(call, timeout=min(REQUEST_TIMEOUT, remaining)) as response:
            body = response.read(MAX_BODY + 1)
            return response.status, dict(response.headers.items()), body
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), b""
    except (OSError, ValueError) as error:
        raise LookupFailed(f"could not reach GitHub: {type(error).__name__}") from error


def from_api(api_base: str, deadline: float) -> tuple[bool, str | None]:
    """Return (answered, tag). `answered` is False when the API is rate
    limited or failing and the web fallback should run."""
    for page in range(1, MAX_PAGES + 1):
        url = (
            f"{api_base.rstrip('/')}/repos/{REPO}/releases"
            f"?per_page={PAGE_SIZE}&page={page}"
        )
        status, _, body = request(url, "GET", deadline)
        if status in FALLBACK_STATUSES or status >= 500:
            return False, None
        if status != 200:
            raise LookupFailed(f"GitHub API answered HTTP {status}")
        if len(body) > MAX_BODY:
            raise LookupFailed("GitHub API answer is too large")
        try:
            releases = json.loads(body)
        except ValueError as error:
            raise LookupFailed("GitHub API answered with malformed JSON") from error
        if not isinstance(releases, list):
            raise LookupFailed("GitHub API answered with malformed JSON")
        tag = latest_tag(releases)
        if tag is not None or len(releases) < PAGE_SIZE:
            return True, tag
    return True, None


def from_redirect(web_base: str, deadline: float) -> str | None:
    """Read the tag from the Location of the web /releases/latest redirect."""
    url = f"{web_base.rstrip('/')}/{REPO}/releases/latest"
    status, headers, _ = request(url, "HEAD", deadline)
    location = {name.lower(): value for name, value in headers.items()}.get("location")
    if status not in (301, 302, 303, 307, 308) or not location:
        raise LookupFailed(f"release page answered HTTP {status} without a redirect")
    path = unquote(urlsplit(location).path).rstrip("/")
    if path.endswith("/releases"):
        return None
    _, marker, tag = path.partition("/releases/tag/")
    if not marker or parse_tag(tag) is None:
        raise LookupFailed(f"latest release {tag!r} is not a sai-cli release")
    return tag


def find_latest(api_base: str = API_BASE, web_base: str = WEB_BASE) -> str | None:
    """Return the latest sai-cli release tag, or None when there is none yet."""
    deadline = time.monotonic() + TOTAL_TIMEOUT
    answered, tag = from_api(api_base, deadline)
    return tag if answered else from_redirect(web_base, deadline)


def wheel_url(tag: str, web_base: str = WEB_BASE) -> str:
    version = tag[len(TAG_PREFIX) :]
    base = f"{web_base.rstrip('/')}/{REPO}/releases/download/{quote(tag)}"
    return f"{base}/sai_cli-{version}-py3-none-any.whl"


def plan(tag: str | None, source: bool, web_base: str = WEB_BASE) -> tuple[str, str]:
    """Return a note for the person and the requirement `uv tool install`
    gets."""
    git = f"git+{WEB_BASE}/{REPO}"
    if tag is None:
        note = "No sai-cli release is published yet; installing from source (main)."
        return note, git
    version = tag[len(TAG_PREFIX) :]
    if source:
        return f"Building sai-cli {version} from source.", f"{git}@{tag}"
    return f"Installing sai-cli {version} from its release wheel.", wheel_url(
        tag, web_base
    )


def find_uv() -> str | None:
    """The uv that runs this script, or the one on PATH."""
    return os.environ.get("UV") or shutil.which("uv")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--source", action="store_true", help="build from Git instead of the wheel"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the command without running it"
    )
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--web-base", default=WEB_BASE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        tag = find_latest(args.api_base, args.web_base)
    except LookupFailed as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            f"To build from source instead: uv tool install git+{WEB_BASE}/{REPO}",
            file=sys.stderr,
        )
        return 1
    note, requirement = plan(tag, args.source, args.web_base)
    print(note)
    uv = find_uv()
    command = [uv or "uv", "tool", "install", requirement]
    print(f"$ {shlex.join(['uv', *command[1:]])}", flush=True)
    if args.dry_run:
        print("Dry run: nothing was installed.")
        return 0
    if uv is None:
        print(
            "error: uv is not on PATH; install it from https://docs.astral.sh/uv/",
            file=sys.stderr,
        )
        return 1
    code = subprocess.run(command, check=False).returncode
    if code != 0:
        print(f"error: uv tool install failed with exit code {code}", file=sys.stderr)
        return code
    print("Installed. Run `sai --help` to start.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
