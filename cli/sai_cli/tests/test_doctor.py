"""`sai doctor`: local and network checks, output, exit codes, no secrets."""

from __future__ import annotations

import base64
import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from sai_cli import doctor
from sai_cli.core import keeper, margin, storage, stream
from sai_cli.mcp_server import tools
from sai_cli.sai_cli import main
from websockets.sync.server import ServerConnection, serve
from websockets.typing import Subprotocol

ADDRESS = "0x" + "4803" * 10
OTHER = "0x" + "c" * 40
ENDPOINT = keeper.MAINNET_HTTP


def b64(value: dict[str, Any]) -> str:
    raw = json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


HEADER = b64({"alg": "ES256K", "typ": "JWT", "kid": "doctor-test"})
PAYLOAD = b64({"sub": ADDRESS, "purpose": "user_intent", "nonce": "q7Zr2"})
SIGNATURE = "c2lnbmF0dXJlLW5ldmVyLXByaW50ZWQtYnktZG9jdG9y"
TOKEN = f"{HEADER}.{PAYLOAD}.{SIGNATURE}"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    for name in ("SAI_ENDPOINT", "SAI_WS_ENDPOINT", "SAI_LCD_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def save(hours: float = 48, endpoint: str = ENDPOINT, address: str = ADDRESS) -> None:
    storage.save_session(
        {
            "token": TOKEN,
            "evmAddress": address,
            "endpoint": endpoint,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
        }
    )


def ctx(endpoint: str = ENDPOINT, **config: Any) -> doctor.Context:
    return doctor.context(tools.Config(endpoint=endpoint, **config), path_env="")


def one(fn, context: doctor.Context) -> doctor.Check:
    result = fn(context)
    assert isinstance(result, doctor.Check)
    return result


def snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ROOT with its mode and file bytes."""
    return {
        str(path): (
            path.stat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(root.rglob("*"))
    }


def run_cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    return result.exit_code, result.output


# Session


def test_empty_home_creates_nothing_and_warns_signed_out(isolated_home):
    code, output = run_cli("doctor", "--offline")
    assert code == 0
    assert list(isolated_home.iterdir()) == []
    assert "Signed out; MCP trading tools are unavailable" in output
    assert "No intent ledger yet" in output
    assert output.count("Skipped with --offline") == 3


def test_valid_session_passes():
    save()
    check = one(doctor.check_session, ctx())
    assert check.status == "pass"
    assert check.details["address"] == ADDRESS


def test_expired_session_fails():
    save(hours=-1)
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "expired" in check.summary
    assert check.fix and "sai auth login" in check.fix


def test_session_for_other_endpoint_fails():
    save(endpoint="https://user:pw@other.example/query?key=abc")
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "https://other.example/query" in check.summary
    assert "pw" not in check.summary and "key=abc" not in check.summary


def test_session_expiring_within_a_day_warns():
    save(hours=0.5)
    check = one(doctor.check_session, ctx())
    assert check.status == "warn"
    assert "expires in" in check.summary and "min" in check.summary


@pytest.mark.parametrize(
    "envelope",
    [{"version": 1, "salt": "x"}, {"version": 2, "backend": "file"}],
)
def test_passphrase_session_fails(isolated_home, envelope):
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "session.json").write_text(json.dumps(envelope))
    (root / "session.json").chmod(0o600)
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "passphrase" in check.summary


def test_unreadable_session_fails(isolated_home):
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "session.json").write_text("not json")
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert "unreadable" in check.summary


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_loose_modes_warn_without_changing_them(isolated_home):
    save()
    root = isolated_home / "trader"
    root.chmod(0o755)
    (root / "session.json").chmod(0o644)
    before = snapshot(isolated_home)
    check = one(doctor.check_session, ctx())
    assert check.status == "warn"
    assert check.fix == (f"chmod 700 {root}; chmod 600 {root / 'session.json'}")
    assert snapshot(isolated_home) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_loose_modes_add_to_a_failing_fix(isolated_home):
    save(hours=-1)
    (isolated_home / "trader" / "session.json").chmod(0o640)
    check = one(doctor.check_session, ctx())
    assert check.status == "fail"
    assert check.fix and "; then chmod 600" in check.fix


# Intents


def ledger(rows: list[tuple[str, str, str, str]], version: int | None = None) -> Path:
    with storage.db() as connection:
        for key, endpoint, account, status in rows:
            connection.execute(
                "INSERT INTO intents (id, endpoint, account, kind, status, entity,"
                " created_at) VALUES (?, ?, ?, 'OPEN_TRADE', ?, '', ?)",
                (key, endpoint, account.lower(), status, time.time()),
            )
        if version is not None:
            connection.execute(f"PRAGMA user_version = {version}")
    return storage.home() / "intents.sqlite3"


def test_clean_ledger_passes():
    ledger([("k1", ENDPOINT, ADDRESS, "executed")])
    check = one(doctor.check_intents, ctx())
    assert check.status == "pass"
    assert check.details["unknown"] == 0


def test_ledger_from_newer_sai_fails_and_is_not_touched(isolated_home):
    path = ledger([], version=storage.SCHEMA_VERSION + 1)
    before = path.read_bytes()
    check = one(doctor.check_intents, ctx())
    assert check.status == "fail"
    assert "schema" in check.summary
    assert path.read_bytes() == before


def test_unknown_intent_for_signed_in_account_fails():
    save()
    ledger(
        [
            ("20261118T000000000000-a", ENDPOINT, ADDRESS, "unknown"),
            ("20261118T000000000000-b", ENDPOINT, OTHER, "unknown"),
        ]
    )
    check = one(doctor.check_intents, ctx())
    assert check.status == "fail"
    assert check.summary == "1 unknown intent blocks trading for 0x4803…"
    assert check.fix == (f"sai intent status {ADDRESS} --key 20261118T000000000000-a")
    assert check.details["otherAccounts"] == {"unknown": 1, "accounts": 1}


def test_unknown_intents_for_other_accounts_warn_with_counts_only():
    save()
    ledger(
        [
            ("k1", ENDPOINT, OTHER, "unknown"),
            ("k2", ENDPOINT, OTHER, "unknown"),
            ("k3", "https://other.example/query", ADDRESS, "unknown"),
        ]
    )
    check = one(doctor.check_intents, ctx())
    assert check.status == "warn"
    assert check.summary == (
        "3 unknown intents for 2 accounts not signed in here; they do not"
        " block this session"
    )
    rendered = json.dumps(check.to_json())
    assert OTHER.lower() not in rendered and "k1" not in rendered


def test_signed_out_unknowns_never_block():
    ledger([("k1", ENDPOINT, ADDRESS, "unknown")])
    assert one(doctor.check_intents, ctx()).status == "warn"


def test_corrupt_ledger_fails(isolated_home):
    root = isolated_home / "trader"
    root.mkdir(mode=0o700)
    (root / "intents.sqlite3").write_bytes(b"not a database" * 512)
    check = one(doctor.check_intents, ctx())
    assert check.status == "fail"
    assert "unreadable" in check.summary


def test_ledger_is_opened_read_only(isolated_home):
    path = ledger([])
    with doctor.open_ledger(path) as connection, pytest.raises(sqlite3.Error):
        connection.execute("CREATE TABLE x (y)")


# Install


def fake_sai(directory: Path) -> Path:
    directory.mkdir()
    path = directory / "sai"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables")
def test_two_installs_on_path_warn(tmp_path):
    first = fake_sai(tmp_path / "a")
    fake_sai(tmp_path / "b")
    path_env = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    context = doctor.context(tools.Config(), path_env=path_env)
    check = one(doctor.check_install, context)
    assert check.status == "warn"
    assert str(first.resolve()) in check.summary
    assert len(check.details["onPath"]) == 2


def test_install_reports_package_version():
    check = one(doctor.check_install, ctx())
    assert check.details["version"] == doctor.info.version()
    assert check.status in ("pass", "warn")


# Network stubs


class Stub:
    def __init__(self, enabled: bool = True, network: str = "cataclysm-1") -> None:
        self.enabled = enabled
        self.network = network


@pytest.fixture
def http_stub() -> Iterator[tuple[str, Stub]]:
    """Serve Keeper's trading route on POST and LCD node_info on GET."""
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value: dict[str, Any]) -> None:
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "oneClickTrading" in body["query"]
            route = {
                "enabled": state.enabled,
                "chainId": "cataclysm-1",
                "contractAddress": "nibi1x",
            }
            self.reply({"data": {"oneClickTrading": route}})

        def do_GET(self):
            assert self.path.endswith("/cosmos/base/tendermint/v1beta1/node_info")
            self.reply({"default_node_info": {"network": state.network}})

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


def ws_stub(ack: bool) -> tuple[str, Any]:
    release = threading.Event()

    def handler(connection: ServerConnection) -> None:
        connection.recv()
        if ack:
            connection.send(json.dumps({"type": "connection_ack"}))
        release.wait(10)

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        release.set()
        server.shutdown()
        thread.join()

    return f"ws://127.0.0.1:{server.socket.getsockname()[1]}/query", stop


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_network_checks_pass_against_stubs(http_stub):
    base, _state = http_stub
    ws, stop = ws_stub(ack=True)
    try:
        checks = doctor.run_checks(
            ctx(f"{base}/query", ws_override=ws, lcd_endpoint=base)
        )
    finally:
        stop()
    by_id = {check.id: check for check in checks}
    assert [check.id for check in checks] == [
        "install",
        "session",
        "intents",
        "keeper",
        "websocket",
        "lcd",
    ]
    assert by_id["keeper"].status == "pass"
    assert by_id["websocket"].status == "pass"
    assert by_id["lcd"].status == "pass"
    assert by_id["lcd"].details["network"] == "cataclysm-1"


def test_trading_disabled_warns(http_stub):
    base, state = http_stub
    state.enabled = False
    check = one(doctor.check_keeper, ctx(f"{base}/query"))
    assert check.status == "warn"
    assert "trading is disabled" in check.summary


def test_lcd_on_another_chain_warns(http_stub):
    base, state = http_stub
    state.network = "nibiru-testnet-2"
    ws, stop = ws_stub(ack=True)
    try:
        checks = doctor.run_checks(
            ctx(f"{base}/query", ws_override=ws, lcd_endpoint=base)
        )
    finally:
        stop()
    lcd = next(check for check in checks if check.id == "lcd")
    assert lcd.status == "warn"
    assert "Keeper trades on cataclysm-1" in lcd.summary


def test_silent_websocket_fails_within_the_timeout():
    ws, stop = ws_stub(ack=False)
    start = time.monotonic()
    try:
        check = one(doctor.check_websocket, ctx(ws_override=ws))
    finally:
        stop()
    assert time.monotonic() - start < 6
    assert check.status == "fail"
    assert "connection_ack" in check.summary


def test_unreachable_endpoints_fail_and_exit_1():
    dead = f"http://127.0.0.1:{free_port()}"
    code, output = run_cli(
        "--endpoint",
        f"{dead}/query",
        "doctor",
        "--lcd-endpoint",
        dead,
    )
    assert code == 1
    for line in ("fail    keeper", "fail    websocket", "fail    lcd"):
        assert line in output


def test_plain_text_remote_endpoint_fails():
    check = one(doctor.check_keeper, ctx("http://keeper.example/query"))
    assert check.status == "fail"
    assert "HTTPS" in check.summary


def test_offline_makes_no_connections(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("doctor --offline opened a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    code, output = run_cli("doctor", "--offline")
    assert code == 0
    assert "skipped" in output


def test_crashing_check_becomes_a_failure(monkeypatch):
    def boom(_ctx):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(doctor, "LOCAL_CHECKS", [("boom", boom)])
    checks = doctor.run_checks(ctx(), offline=True)
    assert checks[0].status == "fail"
    assert TOKEN not in checks[0].summary


# Output


def test_json_report_shape_and_exit_code():
    save(hours=-1)
    code, output = run_cli("--json", "doctor", "--offline")
    assert code == 1
    report = json.loads(output)
    assert report["schemaVersion"] == 1
    assert report["overall"] == "fail"
    assert set(report["sai"]) == {"version", "install"}
    datetime.fromisoformat(report["generatedAt"])
    for check in report["checks"]:
        assert set(check) == {
            "id",
            "status",
            "summary",
            "fix",
            "details",
            "durationMs",
        }
        assert check["status"] in doctor.STATUSES
        assert isinstance(check["durationMs"], int)


def test_overall_ignores_skipped():
    checks = [doctor.Check("a", "pass", ""), doctor.Check("b", "skipped", "")]
    assert doctor.overall(checks) == "pass"
    checks.append(doctor.Check("c", "warn", ""))
    assert doctor.overall(checks) == "warn"


def test_session_token_never_printed(http_stub):
    base, _state = http_stub
    save(hours=-1, endpoint=f"{base}/query?token={TOKEN}")
    ledger([("k1", f"{base}/query", ADDRESS, "unknown")])
    ws, stop = ws_stub(ack=True)
    try:
        outputs = [
            run_cli(
                *prefix,
                "--endpoint",
                f"{base}/query",
                "doctor",
                "--ws-endpoint",
                f"{ws}?token={TOKEN}",
                "--lcd-endpoint",
                f"{base}?token={TOKEN}",
            )[1]
            for prefix in ([], ["--json"])
        ]
    finally:
        stop()
    for output in outputs:
        assert "session" in output
        for secret in (TOKEN, HEADER, PAYLOAD, SIGNATURE):
            assert secret not in output


def test_redaction_covers_rendered_output():
    save()
    check = doctor.Check("x", "fail", f"leaked {TOKEN}", f"fix {TOKEN}")
    assert TOKEN not in doctor.render_text([check])
    assert TOKEN not in doctor.render_json(doctor.report([check]))


# Refactors


@pytest.mark.parametrize(
    ("url", "secure", "ok"),
    [
        ("https://keeper.example/query", "https", True),
        ("http://127.0.0.1:8080/query", "https", True),
        ("http://keeper.example/query", "https", False),
        ("https://u:p@keeper.example/query", "https", False),
        ("wss://keeper.example/query", "wss", True),
        ("ws://localhost/query", "wss", True),
        ("ws://keeper.example/query", "wss", False),
        ("https://keeper.example/query", "wss", False),
    ],
)
def test_check_url(url, secure, ok):
    if ok:
        keeper.check_url(url, "Test", secure)
    else:
        with pytest.raises(ValueError, match="Test"):
            keeper.check_url(url, "Test", secure)


def test_check_ws_refuses_credentials():
    with pytest.raises(stream.StreamError, match="Credentials"):
        stream.check_ws("wss://u:p@keeper.example/query")


def test_request_and_get_json_pass_the_timeout(monkeypatch):
    seen: list[float] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": {}}

    def record(*_args, timeout, **_kwargs):
        seen.append(timeout)
        return Response()

    monkeypatch.setattr(keeper.httpx, "post", record)
    monkeypatch.setattr(margin.httpx, "get", record)
    keeper.request("https://keeper.example/query", keeper.ROUTE)
    keeper.request("https://keeper.example/query", keeper.ROUTE, timeout=5)
    margin._get_json("https://lcd.example/x")
    margin._get_json("https://lcd.example/x", timeout=5)
    assert seen == [20, 5, 20, 5]


def test_handshake_succeeds_on_ack():
    ws, stop = ws_stub(ack=True)
    try:
        stream.handshake(ws, 5)
    finally:
        stop()
