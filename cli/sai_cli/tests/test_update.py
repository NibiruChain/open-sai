"""Read-only update support: tags, latest fetch, install kind, cache, and
`sai update --check` against loopback GitHub stand-ins."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import pytest
from click.testing import CliRunner
from sai_cli.core import update
from sai_cli.sai_cli import (
    EXIT_UNSUPPORTED,
    main,
    package_version,
    print_update_report,
    update_report,
)

API_PATH = f"/repos/{update.REPO}/releases/latest"
WEB_PATH = f"/{update.REPO}/releases/latest"

# path -> (status, headers, body)
Routes = dict[str, tuple[int, dict[str, str], bytes]]


class Stub:
    """A loopback HTTP server answering fixed routes, recording requests."""

    def __init__(
        self,
        routes: Routes,
        delay: float = 0.0,
        drip: float = 0.0,
        drip_paths: frozenset[str] = frozenset(),
    ) -> None:
        """DRIP sends each response on DRIP_PATHS one byte at a time, DRIP
        seconds apart, so no single read waits long enough to time out."""
        self.routes = routes
        self.requests: list[tuple[str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, body: bool) -> None:
                stub.requests.append((self.command, self.path))
                time.sleep(delay)
                status, headers, data = stub.routes.get(self.path, (404, {}, b""))
                if self.path in drip_paths:
                    self.drip(status, headers, data if body else b"")
                    return
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)

            def drip(self, status: int, headers: dict[str, str], data: bytes) -> None:
                lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(data)}"]
                lines += [f"{name}: {value}" for name, value in headers.items()]
                raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + data
                try:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(drip)
                except OSError:
                    pass
                self.close_connection = True

            def do_GET(self) -> None:
                self.answer(body=True)

            def do_HEAD(self) -> None:
                self.answer(body=False)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def release(tag: str) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps({"tag_name": tag, "prerelease": False}).encode()
    return 200, {"Content-Type": "application/json"}, body


def redirect(location: str) -> tuple[int, dict[str, str], bytes]:
    return 302, {"Location": location}, b""


def fetch(stub: Stub, total_timeout: float = update.TOTAL_TIMEOUT) -> str:
    return update.fetch_latest(
        api_base=stub.url, web_base=stub.url, total_timeout=total_timeout
    )


@pytest.fixture(autouse=True)
def sai_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "sai-home"
    monkeypatch.setenv("SAI_HOME", str(home))
    return home


# Tags


def test_parse_tag_accepts_release_tags_only():
    assert update.parse_tag("v0.1.0") == (0, 1, 0)
    assert update.parse_tag("v10.20.30") == (10, 20, 30)
    for tag in ("0.1.0", "v0.1", "v0.2.0-rc.1", "v1.0.0+build", "release", ""):
        assert update.parse_tag(tag) is None


def test_is_newer_compares_numerically_and_ignores_unparseable():
    assert update.is_newer("v0.10.0", "0.9.9")
    assert update.is_newer("v1.0.0", "0.99.99")
    assert not update.is_newer("v0.1.0", "0.1.0")
    assert not update.is_newer("v0.1.0", "0.2.0")
    assert not update.is_newer("v0.3.0-rc.1", "0.2.0")
    assert not update.is_newer("v0.3.0", "0.2.0.dev1")


# Latest release


def test_fetch_reads_the_api_tag(capsys):
    with Stub({API_PATH: release("v0.2.0")}) as stub:
        assert fetch(stub) == "v0.2.0"
    assert stub.requests == [("GET", API_PATH)]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_fetch_falls_back_to_the_web_redirect(status):
    location = f"https://github.com/{update.REPO}/releases/tag/v0.3.1"
    routes: Routes = {API_PATH: (status, {}, b"{}"), WEB_PATH: redirect(location)}
    with Stub(routes) as stub:
        assert fetch(stub) == "v0.3.1"
    assert stub.requests == [("GET", API_PATH), ("HEAD", WEB_PATH)]


def test_fetch_rejects_a_malformed_api_tag():
    with (
        Stub({API_PATH: release("latest-build")}) as stub,
        pytest.raises(update.UpdateError, match="not vMAJOR"),
    ):
        fetch(stub)


def test_fetch_rejects_malformed_json():
    with (
        Stub({API_PATH: (200, {}, b"not json")}) as stub,
        pytest.raises(update.UpdateError, match="malformed JSON"),
    ):
        fetch(stub)


def test_fetch_rejects_a_prerelease_redirect():
    location = f"/{update.REPO}/releases/tag/v0.4.0-rc.1"
    routes: Routes = {API_PATH: (403, {}, b""), WEB_PATH: redirect(location)}
    with Stub(routes) as stub, pytest.raises(update.UpdateError, match="not vMAJOR"):
        fetch(stub)


def test_fetch_rejects_a_redirect_that_is_not_a_tag():
    routes: Routes = {
        API_PATH: (429, {}, b""),
        WEB_PATH: redirect(f"/{update.REPO}/releases"),
    }
    with (
        Stub(routes) as stub,
        pytest.raises(update.UpdateError, match="away from a release tag"),
    ):
        fetch(stub)


def test_fetch_reports_no_release_and_does_not_fall_back_on_404():
    with Stub({}) as stub, pytest.raises(update.UpdateError, match="no release"):
        fetch(stub)
    assert stub.requests == [("GET", API_PATH)]


def test_fetch_gives_up_at_the_total_timeout():
    with Stub({API_PATH: release("v0.2.0")}, delay=1.0) as stub:
        started = time.monotonic()
        with pytest.raises(update.UpdateError, match="timed out"):
            fetch(stub, total_timeout=0.3)
    assert time.monotonic() - started < 0.9


def slow_release() -> tuple[int, dict[str, str], bytes]:
    status, headers, body = release("v0.2.0")
    return status, headers, body + b" " * 200


@pytest.mark.parametrize("fallback", [False, True])
def test_fetch_keeps_its_deadline_against_a_slow_drip(fallback):
    location = f"/{update.REPO}/releases/tag/v0.2.0"
    if fallback:
        routes: Routes = {API_PATH: (429, {}, b""), WEB_PATH: redirect(location)}
        drip_paths = frozenset({WEB_PATH})
    else:
        routes = {API_PATH: slow_release()}
        drip_paths = frozenset({API_PATH})
    with Stub(routes, drip=0.02, drip_paths=drip_paths) as stub:
        started = time.monotonic()
        with pytest.raises(update.UpdateError, match="timed out"):
            fetch(stub, total_timeout=0.3)
        elapsed = time.monotonic() - started
    assert elapsed < 0.6


def test_fetch_reports_an_unreachable_host():
    with Stub({}) as stub:
        url = stub.url
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.fetch_latest(api_base=url, web_base=url)


# Install kind


def make_prefix(root: Path, direct_url: dict | None, receipt: bool = False) -> Path:
    dist = root / "lib" / "python3.11" / "site-packages" / "sai_cli-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Name: sai-cli\nVersion: 0.1.0\n")
    if direct_url is not None:
        (dist / "direct_url.json").write_text(json.dumps(direct_url))
    if receipt:
        (root / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "sai-cli" }]\n'
        )
    return root


GIT = {
    "url": "https://github.com/SaiDotFun/open-sai",
    "vcs_info": {
        "vcs": "git",
        "commit_id": "a" * 40,
        "requested_revision": "v0.1.0",
    },
}


def test_install_kind_uv_tool_from_this_repository(tmp_path):
    prefix = make_prefix(tmp_path / "tools" / "sai-cli", GIT, receipt=True)
    install = update.install_kind(prefix)
    assert install.kind == update.UV_TOOL_GIT
    assert (install.commit, install.requested) == ("a" * 40, "v0.1.0")


def test_install_kind_uv_tool_from_a_fork_directory_or_index(tmp_path):
    fork = {**GIT, "url": "https://github.com/someone/open-sai.git"}
    local = {"url": "file:///home/u/open-sai", "dir_info": {}}
    for name, direct in (("fork", fork), ("dir", local), ("index", None)):
        prefix = make_prefix(tmp_path / name, direct, receipt=True)
        assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER, name


def test_install_kind_checkout_uvx_and_other(tmp_path):
    editable = {"url": "file:///src/open-sai", "dir_info": {"editable": True}}
    checkout = make_prefix(tmp_path / "checkout" / ".venv", editable)
    assert update.install_kind(checkout).kind == update.CHECKOUT
    uvx = make_prefix(tmp_path / "cache" / "archive-v0" / "abc123", GIT)
    assert update.install_kind(uvx).kind == update.UVX
    pip = make_prefix(tmp_path / "venv", None)
    assert update.install_kind(pip).kind == update.OTHER


def test_install_kind_strips_credentials_and_survives_corrupt_files(tmp_path):
    secret = {**GIT, "url": "https://user:token@github.com/SaiDotFun/open-sai"}
    prefix = make_prefix(tmp_path / "secret", secret, receipt=True)
    install = update.install_kind(prefix)
    assert install.kind == update.UV_TOOL_GIT
    assert install.url == "https://github.com/SaiDotFun/open-sai"
    assert "token" not in json.dumps(install.to_json())

    broken = make_prefix(tmp_path / "broken", None, receipt=True)
    dist = next(broken.glob("lib/*/site-packages/sai_cli-*.dist-info"))
    (dist / "direct_url.json").write_text("{not json")
    assert update.install_kind(broken).kind == update.UV_TOOL_OTHER


# Cache


def test_cache_round_trip(sai_home):
    assert update.read_cache() is None
    update.write_cache(update.Cache("v0.2.0", "2026-11-18T00:00:00+00:00"))
    assert update.cache_path() == sai_home / "update.json"
    assert json.loads(update.cache_path().read_text())["schema"] == 1
    assert update.read_cache() == update.Cache("v0.2.0", "2026-11-18T00:00:00+00:00")
    assert update.latest_version() == "0.2.0"


@pytest.mark.parametrize(
    "text",
    [
        "{broken",
        "[]",
        json.dumps({"schema": 2, "latest": "v0.2.0", "checkedAt": "t"}),
        json.dumps({"schema": 1, "latest": "v0.2.0-rc.1", "checkedAt": "t"}),
        json.dumps({"schema": 1, "latest": "v0.2.0"}),
    ],
)
def test_corrupt_cache_counts_as_missing(sai_home, text):
    sai_home.mkdir()
    update.cache_path().write_text(text)
    assert update.read_cache() is None
    assert update.latest_version() is None


# sai update


def invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def test_update_check_json_reports_versions_and_caches(sai_home):
    with Stub({API_PATH: release("v99.0.0")}) as stub:
        result = invoke(
            "--json",
            "update",
            "--check",
            "--api-base",
            stub.url,
            "--web-base",
            stub.url,
        )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["installed"] == package_version()
    assert (report["latest"], report["latestTag"]) == ("99.0.0", "v99.0.0")
    assert report["updateAvailable"] is True
    assert report["install"]["kind"] in {
        update.CHECKOUT,
        update.UV_TOOL_GIT,
        update.UV_TOOL_OTHER,
        update.UVX,
        update.OTHER,
    }
    assert report["update"]["note"]
    assert update.latest_version() == "99.0.0"


def test_update_check_text_when_current():
    current = f"v{package_version()}"
    with Stub({API_PATH: release(current)}) as stub:
        result = invoke(
            "update", "--check", "--api-base", stub.url, "--web-base", stub.url
        )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith(f"installed  {package_version()} (")
    assert lines[1] == f"latest     {package_version()}"
    assert lines[2] == "sai is up to date."
    assert result.stderr == ""


def test_update_check_failure_exits_1_without_stdout_or_cache():
    with Stub({API_PATH: release("garbage")}) as stub:
        result = invoke(
            "--json",
            "update",
            "--check",
            "--api-base",
            stub.url,
            "--web-base",
            stub.url,
        )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "update check failed" in result.stderr
    assert update.read_cache() is None


def test_update_without_check_does_not_install():
    result = invoke("update")
    assert result.exit_code == EXIT_UNSUPPORTED
    assert result.stdout == ""
    assert "cannot install updates yet" in result.stderr
    assert "uv tool install" not in result.stderr


UPSTREAM = update.reinstall_command("v0.2.0")


def advice(install: update.Install, capsys) -> tuple[dict, str]:
    report = update_report("0.1.0", "v0.2.0", install)
    print_update_report(report)
    return report["update"], capsys.readouterr().out


def test_only_an_upstream_uv_tool_gets_the_upstream_reinstall(capsys):
    guide, out = advice(update.Install(update.UV_TOOL_GIT, "/p"), capsys)
    assert guide["command"] == UPSTREAM
    assert "installed  0.1.0 (uv-tool-git)" in out
    assert UPSTREAM in out


@pytest.mark.parametrize(
    ("install", "command", "text"),
    [
        (update.Install(update.CHECKOUT, "/p"), None, "uv sync"),
        (update.Install(update.UVX, "/p"), UPSTREAM, "Only uv tool installs"),
        (
            update.Install(update.UV_TOOL_OTHER, "/p", "file:///home/u/sai"),
            UPSTREAM,
            "Only uv tool installs",
        ),
        (update.Install(update.OTHER, "/p"), UPSTREAM, "Only uv tool installs"),
    ],
)
def test_other_install_kinds_get_their_own_guidance(capsys, install, command, text):
    guide, out = advice(install, capsys)
    assert guide["command"] == command
    assert text in out
    if command is None:
        assert "uv tool install" not in out


def test_no_guidance_when_current():
    report = update_report("0.2.0", "v0.2.0", update.Install(update.UV_TOOL_GIT, "/p"))
    assert report["update"] is None
