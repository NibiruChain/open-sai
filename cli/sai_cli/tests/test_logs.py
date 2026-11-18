"""JSONL logs under $SAI_HOME/logs: format, concurrency, redaction,
retention, and the commands that read them.

Keeper is a real HTTP server on loopback, and the CLI and MCP server run
against a temp SAI_HOME.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sai_cli import doctor
from sai_cli.core import keeper, log, reconcile, settings, storage
from sai_cli.core import session as sessions
from sai_cli.mcp_server import server
from sai_cli.sai_cli import command_path, main

ADDR = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
COMMON = set(log.COMMON)
# Secrets planted in URLs; none may reach a log line.
URL_PASSWORD = "hunter2-url-password"
URL_KEY = "url-api-key-0123456789"
OPAQUE_TOKEN = "opaque-session-token-0123456789abcdef"
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    for name in ("SAI_ENDPOINT", "SAI_WS_ENDPOINT", "SAI_LCD_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    log.reset()
    return tmp_path


def jwt(address: str = ADDR, hours: float = 1) -> str:
    """A token shaped like Keeper's: three base64url segments."""

    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    expiry = int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())
    payload = {"sub": address, "exp": expiry, "purpose": "user_intent"}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.{'s' * 43}"


def read_logs(root: Path, surface: str | None = None) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for _day, found, path in log.log_files(root / "logs"):
        if surface in (None, found):
            lines += [json.loads(raw) for raw in path.read_text().splitlines()]
    return lines


def raw_logs(root: Path) -> str:
    return "".join(
        path.read_text() for _day, _surface, path in log.log_files(root / "logs")
    )


def events(root: Path, name: str) -> list[dict[str, Any]]:
    return [line for line in read_logs(root) if line["event"] == name]


def run_cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    return result.exit_code, result.output


class Keeper:
    """What the stub Keeper answers, and the requests it received."""

    def __init__(self) -> None:
        self.intents: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] | None = None
        self.status = 200
        self.paths: list[str] = []
        self.authorizations: list[str | None] = []


@pytest.fixture
def keeper_stub() -> Iterator[tuple[str, Keeper]]:
    """A Keeper on loopback answering prices, markets, and intent records."""
    state = Keeper()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.paths.append(self.path)
            state.authorizations.append(self.headers.get("Authorization"))
            query = body["query"]
            if state.errors is not None:
                value: dict[str, Any] = {"errors": state.errors}
            elif "userIntents" in query:
                value = {"data": {"userIntents": state.intents}}
            elif "tokenPricesUsd" in query:
                value = {"data": {"oracle": {"tokenPricesUsd": []}}}
            elif "tradeHistory" in query:
                value = {"data": {"perp": {"tradeHistory": []}}}
            else:
                value = {"data": {"perp": {"borrowings": [], "trades": []}}}
            data = json.dumps(value).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/query", state
    finally:
        httpd.shutdown()
        httpd.server_close()


def save_session(endpoint: str, address: str = ADDR, token: str | None = None) -> str:
    token = token or jwt(address)
    storage.save_session(
        {
            "token": token,
            "evmAddress": address,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    )
    return token


def call(mcp: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool through an in-memory MCP client and decode its JSON."""

    async def run() -> Any:
        async with create_connected_server_and_client_session(
            mcp._mcp_server
        ) as client:
            return await client.call_tool(name, arguments or {})

    block = asyncio.run(run()).content[0]
    assert isinstance(block, TextContent)
    return json.loads(block.text)


# Line format


def test_event_writes_one_json_line_with_the_common_fields(home):
    log.event("keeper.request", operation="prices", durationMs=12, skipped=None)
    path = log.file_path("cli", datetime.now(UTC).date(), home / "logs")
    raw = path.read_text()
    assert raw.endswith("\n") and raw.count("\n") == 1
    line = json.loads(raw)
    assert set(line) == COMMON | {"operation", "durationMs"}
    assert line["event"] == "keeper.request" and line["level"] == "info"
    assert line["surface"] == "cli" and line["pid"] == os.getpid()
    assert line["runId"] == log.RUN_ID and len(log.RUN_ID) == 32
    assert line["version"] == log.VERSION
    stamp = datetime.fromisoformat(line["ts"])
    assert line["ts"].endswith("Z") and stamp.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=1)


@posix_only
def test_directory_and_files_are_private(home):
    with log.bind(surface="mcp"):
        log.event("mcp.tool_call", tool="market_prices")
    root = home / "logs"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    names = sorted(path.name for path in root.iterdir())
    assert names == [".pruned", f"mcp-{datetime.now(UTC).date().isoformat()}.jsonl"]
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_bound_fields_reach_every_event_and_pick_the_surface(home):
    with log.bind(surface="mcp", tool="trade_open", callId="abc"):
        log.event("keeper.request", operation="submit")
        with log.bind(surface="update"):
            log.event("update.check", ok=True)
    log.event("command.start")
    assert [(line["surface"], line["event"]) for line in read_logs(home)] == [
        ("cli", "command.start"),
        ("mcp", "keeper.request"),
        ("update", "update.check"),
    ]
    request = events(home, "keeper.request")[0]
    assert (request["tool"], request["callId"]) == ("trade_open", "abc")
    assert "tool" not in events(home, "command.start")[0]


def test_long_values_are_cut_and_oversized_lines_keep_the_common_fields(home):
    log.event("big.text", text="x" * 5000)
    log.event("big.line", items=["y" * 1500 for _ in range(40)])
    text, line = read_logs(home)
    assert len(text["text"]) < 2100 and "3000 more characters" in text["text"]
    assert set(line) == COMMON | {"truncated"} and line["truncated"] == ["items"]
    assert all(len(raw) <= log.MAX_LINE for raw in raw_logs(home).splitlines())


WRITER = """
import sys
from sai_cli.core import log
worker, count = int(sys.argv[1]), int(sys.argv[2])
for index in range(count):
    log.event("stress", worker=worker, index=index, pad="p" * (index * 37 % 1900))
"""


def test_concurrent_processes_write_whole_lines(home):
    workers, count = 12, 150
    env = {**os.environ, "SAI_HOME": str(home)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(worker), str(count)], env=env
        )
        for worker in range(workers)
    ]
    assert [process.wait(timeout=120) for process in processes] == [0] * workers
    raw = raw_logs(home)
    lines = [json.loads(line) for line in raw.splitlines()]
    assert len(lines) == workers * count
    seen = {(line["worker"], line["index"]) for line in lines}
    assert seen == {(w, i) for w in range(workers) for i in range(count)}
    assert all(len(line["pad"]) == line["index"] * 37 % 1900 for line in lines)
    assert len({line["pid"] for line in lines}) == workers
    assert len({line["runId"] for line in lines}) == workers


# Redaction


def test_strings_lose_stored_tokens_and_anything_shaped_like_a_jwt(home):
    stored = save_session(keeper.MAINNET_HTTP)
    save_session(keeper.MAINNET_HTTP, OTHER, OPAQUE_TOKEN)
    unsaved = jwt(OTHER)
    log.event(
        "probe",
        error=f"failed with {stored} and {OPAQUE_TOKEN}",
        nested={"values": [unsaved, {OPAQUE_TOKEN: stored}]},
    )
    raw = raw_logs(home)
    assert raw.count(log.REDACTED) >= 5
    for secret in (stored, OPAQUE_TOKEN, unsaved):
        assert secret not in raw


def test_argument_drops_url_credentials():
    url = f"https://user:{URL_PASSWORD}@keeper.example:8443/query?key={URL_KEY}#frag"
    assert log.argument(url) == "https://keeper.example:8443/query"
    assert log.argument(f"--endpoint={url}") == (
        "--endpoint=https://keeper.example:8443/query"
    )
    assert log.argument("--json") == "--json"
    assert log.argument(ADDR) == ADDR


def test_no_token_or_url_credential_reaches_a_log_line(home, keeper_stub):
    """Run CLI commands and MCP tools with a signed-in session, a Keeper URL
    that carries an API key, and the token pasted where it does not belong;
    then scan every log line."""
    base, state = keeper_stub
    endpoint = f"{base}?api_key={URL_KEY}"
    with_password = endpoint.replace("http://", f"http://user:{URL_PASSWORD}@")
    token = save_session(endpoint)
    save_session(endpoint, OTHER, OPAQUE_TOKEN)
    storage.use_session(ADDR)
    storage.pending("KEY1", endpoint, ADDR, "OPEN_TRADE", "")

    assert run_cli("--endpoint", endpoint, "market", "prices")[0] == 0
    assert run_cli(f"--endpoint={endpoint}", "auth", "status")[0] == 0
    assert run_cli("--endpoint", endpoint, "auth", "list")[0] == 0
    status = run_cli("--endpoint", endpoint, "intent", "status", ADDR, "--key", "KEY1")
    assert status[0] == 0
    # Mistakes: credentials in the URL, and the token as an argument.
    assert run_cli("--endpoint", with_password, "market", "list")[0] == 1
    assert run_cli("--endpoint", endpoint, "account", "positions", token)[0] == 0
    assert run_cli("--endpoint", endpoint, "auth", "use", OPAQUE_TOKEN)[0] == 1
    assert run_cli("--endpoint", endpoint, "doctor", "--offline")[0] in (0, 1)

    mcp = server.create_server(settings.load({"keeper.endpoint": endpoint}))
    assert call(mcp, "mcp_info")["trading"]["available"] is True
    assert call(mcp, "auth_status")["address"] == ADDR
    assert "accounts" in call(mcp, "auth_list")
    assert "oracle" in call(mcp, "market_prices")
    assert "userIntents" in call(mcp, "intent_status", {"key": token})
    assert "code" in call(mcp, "auth_use", {"address": OPAQUE_TOKEN})
    assert "code" in call(mcp, "trade_close", {"trade_id": 1, "preview_digest": token})
    with pytest.raises(ToolError, match="preview_digest"):
        asyncio.run(mcp.call_tool("trade_close", {"trade_id": token}))
    assert call(mcp, "auth_use", {"address": OTHER})["accounts"]
    storage.set_status("KEY1", endpoint, ADDR, "accepted")
    storage.delete_session(OTHER)

    assert f"Bearer {token}" not in state.authorizations  # reads send no token
    assert all(URL_KEY in path for path in state.paths)
    raw = raw_logs(home)
    lines = read_logs(home)
    assert {line["surface"] for line in lines} == {"cli", "mcp"}
    seen = {line["event"] for line in lines}
    assert seen >= {
        "command.start",
        "command.end",
        "mcp.tool_call",
        "mcp.tool_result",
        "keeper.request",
        "intent.reserved",
        "intent.status",
        "intent.keeper",
        "session.saved",
        "session.active",
        "session.removed",
        "doctor.result",
    }
    for secret in (token, OPAQUE_TOKEN, URL_KEY, URL_PASSWORD, "api_key", "user:"):
        assert secret not in raw, secret
    assert log.REDACTED in raw
    endpoints = {line["endpoint"] for line in lines if "endpoint" in line}
    assert endpoints == {base}


# What each event records


def test_command_start_and_end_record_argv_exit_code_and_duration(home, keeper_stub):
    base, _state = keeper_stub
    assert run_cli("--endpoint", base, "--json", "market", "prices")[0] == 0
    start, request, end = read_logs(home)
    assert start["event"] == "command.start" and start["command"] == "market prices"
    assert start["argv"] == ["--endpoint", base, "--json", "market", "prices"]
    assert request["event"] == "keeper.request"
    assert request["command"] == "market prices"
    assert request["operation"] == "prices" and request["status"] == "ok"
    assert request["httpStatus"] == 200 and request["endpoint"] == base
    assert request["authenticated"] is False and request["durationMs"] >= 0
    assert end["event"] == "command.end" and end["exitCode"] == 0
    assert end["level"] == "info" and end["durationMs"] >= request["durationMs"]
    assert start["runId"] == end["runId"]


def test_a_failed_command_logs_its_exit_code_and_error(home):
    code, _output = run_cli("--endpoint", "http://127.0.0.1:1/query", "market", "list")
    assert code == 1
    request = events(home, "keeper.request")[0]
    assert request["status"] == "not_sent" and request["level"] == "warn"
    assert "httpStatus" not in request
    end = events(home, "command.end")[0]
    assert end["exitCode"] == 1 and end["level"] == "warn"
    assert end["error"] == "Could not connect to Keeper; the request was not sent"
    # A usage error exits 2 before any command runs.
    assert run_cli("market", "nope")[0] == 2
    usage = events(home, "command.end")[1]
    assert usage["exitCode"] == 2 and usage["command"] == "market"
    assert "No such command" in usage["error"]


def test_command_path_skips_options_and_their_values():
    assert command_path(main, ["--endpoint", "market", "market", "prices"]) == [
        "market",
        "prices",
    ]
    assert command_path(main, ["--json", "--endpoint=x", "auth", "use", "0xabc"]) == [
        "auth",
        "use",
    ]
    assert command_path(main, ["mcp", "serve", "--endpoint", "x"]) == ["mcp", "serve"]
    assert command_path(main, []) == []
    assert command_path(main, ["nope", "market"]) == []


def test_help_version_and_skill_write_nothing(home):
    for args in (["--help"], ["--version"], ["--skill"], ["market", "--help"]):
        assert run_cli(*args)[0] == 0
    assert not (home / "logs").exists()


def test_keeper_errors_record_what_keeper_answered(home, keeper_stub):
    base, state = keeper_stub
    state.errors = [
        {"message": "market is paused", "extensions": {"code": "MARKET_PAUSED"}}
    ]
    with pytest.raises(keeper.KeeperError):
        keeper.request(base, keeper.POSITIONS, {"trader": ADDR}, token=jwt())
    state.errors, state.status = None, 503
    with pytest.raises(keeper.KeeperError):
        keeper.request(base, "query { custom }")
    rejected, unavailable = events(home, "keeper.request")
    assert rejected["operation"] == "positions" and rejected["status"] == "error"
    assert rejected["addr"] == ADDR and rejected["authenticated"] is True
    assert rejected["keeperErrors"] == [
        {"code": "MARKET_PAUSED", "message": "market is paused"}
    ]
    assert rejected["error"] == "Keeper rejected the request; check inputs and session"
    assert unavailable["operation"] == "custom" and unavailable["httpStatus"] == 503


def test_intent_lifecycle_is_logged_from_reservation_to_terminal(home, keeper_stub):
    base, state = keeper_stub
    storage.pending("KEY1", base, ADDR, "CLOSE_POSITION", "7")
    record = {
        "intentKey": "KEY1",
        "kind": "CLOSE_POSITION",
        "lifecycleStep": "exec_req_pass",
        "execActor": "server",
        "evmAddress": ADDR,
        "txHash": None,
        "errorCode": None,
        "errorMsg": None,
    }
    state.intents = [record]
    reconcile.reconcile(base, ADDR, "KEY1")
    reconcile.reconcile(base, ADDR, "KEY1")  # No change: no second status line.
    state.intents = [
        {
            **record,
            "lifecycleStep": "exec_fail",
            "errorCode": "SLIPPAGE",
            "errorMsg": "price moved",
        }
    ]
    reconcile.reconcile(base, ADDR, "KEY1")
    trail = [line for line in read_logs(home) if line["event"].startswith("intent.")]
    assert [
        (line["event"], line.get("status") or line.get("lifecycleStep"))
        for line in trail
    ] == [
        ("intent.reserved", "unknown"),
        ("intent.keeper", "exec_req_pass"),
        ("intent.status", "accepted"),
        ("intent.keeper", "exec_req_pass"),
        ("intent.keeper", "exec_fail"),
        ("intent.status", "terminal"),
    ]
    assert all(line["intentKey"] == "KEY1" for line in trail)
    assert all(line["addr"] == ADDR and line["endpoint"] == base for line in trail)
    reserved, _, accepted, _, failed, terminal = trail
    assert reserved["kind"] == "CLOSE_POSITION" and reserved["tradeId"] == "7"
    assert (accepted["previous"], accepted["kind"]) == ("unknown", "CLOSE_POSITION")
    assert (failed["errorCode"], failed["errorMsg"]) == ("SLIPPAGE", "price moved")
    assert terminal["previous"] == "accepted"


def test_an_intent_keeper_never_recorded_is_logged_absent(home, keeper_stub):
    base, _state = keeper_stub
    storage.pending("KEY2", base, ADDR, "OPEN_TRADE", "")
    with storage.db() as connection:
        connection.execute("UPDATE intents SET created_at = 0 WHERE id = 'KEY2'")
    assert reconcile.reconcile(base, ADDR, "KEY2")["localStatus"] == "absent"
    missing = events(home, "intent.keeper")[0]
    assert missing["found"] is False and "lifecycleStep" not in missing
    status = events(home, "intent.status")[0]
    assert (status["previous"], status["status"]) == ("unknown", "absent")


def test_session_events_name_the_addr_and_never_the_token(home):
    token = save_session(keeper.MAINNET_HTTP)
    save_session(keeper.MAINNET_HTTP, OTHER)
    storage.use_session(ADDR)
    storage.delete_session(OTHER)
    expired = {**storage.load_session(), "expiresAt": "2020-01-01T00:00:00+00:00"}
    with pytest.raises(ValueError, match="expired"):
        sessions.check_active(expired, keeper.MAINNET_HTTP)
    lines = [line for line in read_logs(home) if line["event"].startswith("session.")]
    assert [(line["event"], line["addr"]) for line in lines] == [
        ("session.saved", ADDR),
        ("session.saved", OTHER),
        ("session.active", ADDR),
        ("session.removed", OTHER),
        ("session.expired", ADDR),
    ]
    assert lines[1]["connectedAccounts"] == 2 and lines[3]["connectedAccounts"] == 1
    assert lines[0]["endpoint"] == keeper.MAINNET_HTTP
    assert lines[4]["level"] == "warn"
    assert token not in raw_logs(home)


def test_mcp_tool_calls_log_arguments_result_code_and_account(home, keeper_stub):
    base, _state = keeper_stub
    save_session(base)
    mcp = server.create_server(settings.load({"keeper.endpoint": base}))
    assert "oracle" in call(mcp, "market_prices")
    assert call(mcp, "account_positions", {"address": OTHER})["address"] == OTHER
    refused = call(mcp, "trade_close", {"trade_id": 3, "address": OTHER})
    assert refused["code"] == "SESSION_UNAVAILABLE"
    lines = read_logs(home, "mcp")
    names = [line["event"] for line in lines]
    assert names == [
        "mcp.tool_call",
        "keeper.request",
        "mcp.tool_result",
        "mcp.tool_call",
        "keeper.request",
        "mcp.tool_result",
        "mcp.tool_call",
        "mcp.tool_result",
    ]
    prices_call, prices_request, prices_result = lines[:3]
    assert prices_call["tool"] == "market_prices" and prices_call["arguments"] == {}
    assert len({line["callId"] for line in lines[:3]}) == 1
    assert prices_request["tool"] == "market_prices"
    assert prices_result["ok"] is True and prices_result["durationMs"] >= 0
    assert lines[3]["arguments"] == {"address": OTHER}
    assert lines[5]["account"] == OTHER and lines[4]["addr"] == OTHER
    assert lines[3]["callId"] != prices_call["callId"]
    result = lines[7]
    assert result["tool"] == "trade_close" and result["level"] == "warn"
    assert result["ok"] is False and result["code"] == "SESSION_UNAVAILABLE"
    assert "is not connected" in result["error"]


def test_arguments_fastmcp_rejects_are_logged_as_a_tool_error(home):
    save_session(keeper.MAINNET_HTTP)
    mcp = server.create_server(settings.load({}))
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool("trade_close", {"trade_id": "seven"}))
    started, result = read_logs(home, "mcp")
    assert started["arguments"] == {"trade_id": "seven"}
    assert result["event"] == "mcp.tool_result" and result["level"] == "error"
    assert result["ok"] is False and result["code"] == "TOOL_ERROR"
    assert "trade_id" in result["error"] and result["callId"] == started["callId"]


def test_mcp_info_reports_the_log_directory(home, monkeypatch):
    mcp = server.create_server(settings.load({}))
    assert call(mcp, "mcp_info")["logs"] == {"dir": str(home / "logs"), "enabled": True}
    monkeypatch.setenv("SAI_LOG", "0")
    assert call(mcp, "mcp_info")["logs"]["enabled"] is False


def test_update_check_logs_to_the_update_surface(home):
    code, _output = run_cli(
        "update",
        "--check",
        "--api-base",
        "http://127.0.0.1:1",
        "--web-base",
        "http://127.0.0.1:1",
    )
    assert code == 1
    lines = read_logs(home, "update")
    assert [line["event"] for line in lines] == [
        "command.start",
        "update.check",
        "command.end",
    ]
    assert lines[1]["ok"] is False and lines[2]["exitCode"] == 1
    assert read_logs(home, "cli") == []


def test_doctor_logs_its_outcome_and_no_fix_logs_nothing(home):
    assert run_cli("doctor", "--offline", "--no-fix")[0] == 0
    assert list(home.iterdir()) == []
    assert run_cli("doctor", "--offline")[0] == 0
    result = events(home, "doctor.result")[0]
    assert result["overall"] == "warn" and result["offline"] is True
    by_id = {check["id"]: check for check in result["checks"]}
    assert list(by_id) == [
        "install",
        "home",
        "logs",
        "session",
        "intents",
        "keeper",
        "websocket",
        "lcd",
    ]
    assert by_id["home"]["status"] == "fixed" and by_id["home"]["changes"]
    assert set(by_id["logs"]) == {"id", "status", "durationMs"}
    assert by_id["logs"]["status"] == "pass"
    assert "Signed out" in by_id["session"]["summary"]


# Retention


def test_expired_files_keeps_today_and_the_days_before_it():
    today = date(2026, 11, 18)
    files = [
        (today - timedelta(days=age), "cli", Path(f"cli-{age}.jsonl"))
        for age in (0, 1, 13, 14, 40)
    ]
    assert [p.name for p in log.expired_files(files, today, 14)] == [
        "cli-14.jsonl",
        "cli-40.jsonl",
    ]
    assert [p.name for p in log.expired_files(files, today, 1)] == [
        "cli-1.jsonl",
        "cli-13.jsonl",
        "cli-14.jsonl",
        "cli-40.jsonl",
    ]


def old_file(root: Path, surface: str, age: int) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log.file_path(surface, datetime.now(UTC).date() - timedelta(days=age), root)
    path.write_text('{"event":"old"}\n')
    return path


def test_first_event_deletes_old_files_at_most_once_a_day(home):
    root = home / "logs"
    kept = [old_file(root, "cli", 13), old_file(root, "mcp", 1)]
    gone = [old_file(root, "cli", 14), old_file(root, "update", 90)]
    other = root / "notes.txt"
    other.write_text("not a log file")
    log.event("first")
    assert all(path.exists() for path in [*kept, other])
    assert not any(path.exists() for path in gone)
    # The marker says a prune ran today, so a new process skips it.
    late = old_file(root, "cli", 30)
    log.reset()
    log.event("second")
    assert late.exists()
    yesterday = datetime.now(UTC).timestamp() - log.PRUNE_INTERVAL_SECONDS - 60
    os.utime(root / log.PRUNE_MARKER, (yesterday, yesterday))
    log.reset()
    log.event("third")
    assert not late.exists()


def test_keep_days_comes_from_the_environment_or_the_config_file(home, monkeypatch):
    root = home / "logs"
    week_old = old_file(root, "cli", 7)
    monkeypatch.setenv("SAI_LOG_KEEP_DAYS", "30")
    log.event("first")
    assert week_old.exists()
    monkeypatch.delenv("SAI_LOG_KEEP_DAYS")
    assert run_cli("config", "set", "logs.keep_days", "3")[0] == 0
    assert log.config() == (True, 3)
    (root / log.PRUNE_MARKER).unlink()
    log.reset()
    log.event("second")
    assert not week_old.exists()


# Settings


def test_logs_settings_resolve_and_validate(home):
    resolved = settings.resolve({}, {}, {})
    assert (resolved.logs_enabled, resolved.logs_keep_days) == (True, 14)
    env = {"SAI_LOG": "off", "SAI_LOG_KEEP_DAYS": " 30 "}
    resolved = settings.resolve({}, env, {"logs.keep_days": 5})
    assert (resolved.logs_enabled, resolved.logs_keep_days) == (False, 30)
    assert resolved.sources["logs.keep_days"] == "env"
    assert settings.resolve({}, {}, {"logs.keep_days": 5}).value("logs.keep_days") == 5
    for bad in ("0", "-1", "1.5", "many", ""):
        with pytest.raises(settings.SettingsError, match="whole number"):
            settings.parse_value("logs.keep_days", bad)
    for bad in (True, 0, "7", 2.5):
        with pytest.raises(settings.SettingsError, match="whole number"):
            settings.check_value("logs.keep_days", bad)
    text, value = settings.set_value(None, "logs.keep_days", "21")
    assert value == 21 and "\nkeep_days = 21\n" in text
    assert settings.parse_file(text, "config.toml")[0] == {"logs.keep_days": 21}
    assert "# keep_days = 14" in settings.unset_value(text, "logs.keep_days")[0]


def test_disabled_by_environment_writes_nothing(home, monkeypatch, keeper_stub):
    base, _state = keeper_stub
    monkeypatch.setenv("SAI_LOG", "0")
    log.event("nothing")
    assert run_cli("--endpoint", base, "market", "prices")[0] == 0
    save_session(base)
    mcp = server.create_server(settings.load({"keeper.endpoint": base}))
    assert "oracle" in call(mcp, "market_prices")
    assert not (home / "logs").exists()
    monkeypatch.setenv("SAI_LOG", "1")
    log.event("something")
    assert [line["event"] for line in read_logs(home)] == ["something"]


def test_disabled_in_the_config_file_writes_nothing_more(home, keeper_stub):
    base, _state = keeper_stub
    assert run_cli("config", "set", "logs.enabled", "false")[0] == 0
    before = raw_logs(home)
    assert "config set" in before
    assert run_cli("--endpoint", base, "market", "prices")[0] == 0
    assert raw_logs(home) == before
    code, output = run_cli("--json", "logs", "path")
    assert code == 0 and json.loads(output)["enabled"] is False


def test_a_broken_config_file_does_not_stop_logging(home):
    (home / "config.toml").write_text("not = [toml")
    log.event("still.logged")
    assert [line["event"] for line in read_logs(home)] == ["still.logged"]


# Failures to log


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
)
def test_an_unwritable_logs_directory_does_not_break_commands(home, keeper_stub):
    base, _state = keeper_stub
    root = home / "logs"
    root.mkdir(mode=0o500)
    try:
        code, output = run_cli("--endpoint", base, "--json", "market", "prices")
        assert code == 0
        assert json.loads(output) == {"oracle": {"tokenPricesUsd": []}}
        save_session(base)
        mcp = server.create_server(settings.load({"keeper.endpoint": base}))
        assert "oracle" in call(mcp, "market_prices")
        assert list(root.iterdir()) == []
    finally:
        root.chmod(0o700)


def test_a_file_in_place_of_the_logs_directory_does_not_break_commands(
    home, keeper_stub
):
    base, _state = keeper_stub
    (home / "logs").write_text("in the way")
    code, output = run_cli("--endpoint", base, "--json", "market", "prices")
    assert code == 0 and "tokenPricesUsd" in output
    assert (home / "logs").read_text() == "in the way"
    assert run_cli("logs", "tail") == (0, "")


@posix_only
def test_a_symlinked_log_file_is_not_written_through(home):
    target = home / "elsewhere.txt"
    target.write_text("")
    root = home / "logs"
    root.mkdir(mode=0o700)
    log.file_path("cli", datetime.now(UTC).date(), root).symlink_to(target)
    log.event("probe")
    assert target.read_text() == ""


# sai mcp serve


def test_sai_mcp_serve_logs_tool_calls_and_keeps_stdout_protocol_only(home):
    """Drive a real `sai mcp serve` over stdio: every stdout line is the
    JSON-RPC reply to a request, and the calls land in the mcp log file."""
    script = Path(sys.executable).with_name("sai.exe" if os.name == "nt" else "sai")
    command = [str(script)] if script.exists() else [sys.executable, "-m", "sai_cli"]
    endpoint = f"http://127.0.0.1:9/query?api_key={URL_KEY}"

    def rpc(message_id: int | None, method: str, params: dict[str, Any]) -> str:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if message_id is not None:
            message["id"] = message_id
        return json.dumps(message) + "\n"

    with subprocess.Popen(
        [*command, "mcp", "serve", "--endpoint", endpoint],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"SAI_HOME": str(home), "PATH": os.defpath},
        text=True,
    ) as process:
        assert process.stdin is not None and process.stdout is not None

        def ask(message_id: int, method: str, params: dict[str, Any]) -> Any:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(rpc(message_id, method, params))
            process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            assert reply["jsonrpc"] == "2.0" and reply["id"] == message_id
            return reply["result"]

        client = {"name": "test", "version": "0"}
        ask(
            1,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client},
        )
        process.stdin.write(rpc(None, "notifications/initialized", {}))
        info = ask(2, "tools/call", {"name": "mcp_info", "arguments": {}})
        prices = ask(3, "tools/call", {"name": "market_prices", "arguments": {}})
        process.stdin.close()
        rest = process.stdout.read()
        stderr = process.stderr.read() if process.stderr else ""
        assert process.wait(timeout=30) == 0, stderr
    assert rest == ""
    assert json.loads(info["content"][0]["text"])["logs"]["dir"] == str(home / "logs")
    assert json.loads(prices["content"][0]["text"])["code"] == "NOT_SENT"
    lines = read_logs(home)
    assert {line["surface"] for line in lines} == {"mcp"}
    assert [(line["event"], line.get("tool")) for line in lines] == [
        ("command.start", None),
        ("mcp.server_start", None),
        ("mcp.tool_call", "mcp_info"),
        ("mcp.tool_result", "mcp_info"),
        ("mcp.tool_call", "market_prices"),
        ("keeper.request", "market_prices"),
        ("mcp.tool_result", "market_prices"),
        ("command.end", None),
    ]
    assert lines[0]["command"] == "mcp serve" and lines[-1]["exitCode"] == 0
    assert lines[1]["keeper"] == "http://127.0.0.1:9/query"
    assert lines[5]["status"] == "not_sent" and lines[6]["code"] == "NOT_SENT"
    assert len({line["runId"] for line in lines}) == 1
    assert URL_KEY not in raw_logs(home)


# sai logs


def test_logs_path_prints_the_directory(home):
    assert run_cli("logs", "path") == (0, f"{home / 'logs'}\n")
    code, output = run_cli("--json", "logs", "path")
    assert code == 0
    assert json.loads(output) == {
        "dir": str(home / "logs"),
        "enabled": True,
        "keepDays": 14,
        "files": [],
    }
    assert not (home / "logs").exists()
    log.event("probe")
    today = datetime.now(UTC).date().isoformat()
    files = json.loads(run_cli("--json", "logs", "path")[1])["files"]
    assert files == [f"cli-{today}.jsonl"]


def test_logs_tail_filters_by_count_surface_and_event(home):
    old_file(home / "logs", "cli", 1).write_text(
        '{"ts":"2020-01-01T00:00:00.000Z","level":"info","event":"old.event",'
        '"surface":"cli","pid":1,"version":"0","runId":"r"}\nnot json\n'
    )
    log.event("command.start", command="market prices", argv=["market", "prices"])
    with log.bind(surface="mcp", tool="market_prices"):
        log.event("mcp.tool_call", arguments={"address": ADDR})
        log.event("keeper.request", "warn", operation="prices", status="not_sent")
    log.event("intent.reserved", intentKey="KEY1")
    log.event("intent.status", intentKey="KEY1", status="accepted")
    before = raw_logs(home)

    def names(*args: str) -> list[str]:
        code, output = run_cli("--json", "logs", "tail", *args)
        assert code == 0
        return [json.loads(line)["event"] for line in output.splitlines()]

    assert names() == [
        "old.event",
        "command.start",
        "mcp.tool_call",
        "keeper.request",
        "intent.reserved",
        "intent.status",
    ]
    assert names("-n", "2") == ["intent.reserved", "intent.status"]
    assert names("--surface", "mcp") == ["mcp.tool_call", "keeper.request"]
    assert names("--event", "intent") == ["intent.reserved", "intent.status"]
    assert names("--event", "intent.status") == ["intent.status"]
    assert names("--event", "int") == []
    assert names("--surface", "cli", "--event", "keeper") == []
    assert names("--surface", "update") == []
    assert run_cli("logs", "tail", "-n", "0")[0] == 2
    assert run_cli("logs", "tail", "--surface", "web")[0] == 2
    # Reading the log adds nothing to it.
    assert raw_logs(home) == before


def test_logs_tail_plain_output_is_one_line_per_event(home):
    with log.bind(surface="mcp", tool="market_prices"):
        log.event("mcp.tool_call", arguments={"address": ADDR})
    log.event("keeper.request", "warn", operation="prices", ok=False, durationMs=7)
    code, output = run_cli("logs", "tail")
    assert code == 0
    first, second = output.splitlines()
    stamp, surface, level, name, *fields = first.split(" ")
    assert datetime.fromisoformat(stamp) and (surface, level) == ("mcp", "info")
    assert name == "mcp.tool_call"
    assert fields == ["tool=market_prices", f'arguments={{"address":"{ADDR}"}}']
    assert second.split(" ")[1:] == [
        "cli",
        "warn",
        "keeper.request",
        "operation=prices",
        "ok=false",
        "durationMs=7",
    ]


# sai doctor


@posix_only
def test_doctor_makes_the_logs_directory_and_files_private(home):
    log.event("probe")
    root = home / "logs"
    path = log.file_path("cli", datetime.now(UTC).date(), root)
    root.chmod(0o755)
    path.chmod(0o644)
    report = doctor.check_logs(doctor.context(settings.load({}), "", fix=False))
    assert report.status == "warn" and f"chmod go-rwx {path}" in (report.fix or "")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    fixed = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert fixed.status == "fixed" and len(fixed.changes) == 2
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert again.status == "pass" and again.details["files"] == 1


@posix_only
def test_doctor_does_not_follow_a_symlinked_logs_directory(home):
    elsewhere = home / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (home / "logs").symlink_to(elsewhere)
    check = doctor.check_logs(doctor.context(settings.load({}), "", fix=True))
    assert check.status == "fail" and "symlink" in check.summary
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755


def test_doctor_reports_no_logs_yet_and_logging_off(home, monkeypatch):
    context = doctor.context(settings.load({}), "", fix=True)
    check = doctor.check_logs(context)
    assert check.status == "pass" and check.summary == f"No logs yet at {home / 'logs'}"
    assert not (home / "logs").exists()
    monkeypatch.setenv("SAI_LOG", "0")
    assert "logging is off" in doctor.check_logs(context).summary
