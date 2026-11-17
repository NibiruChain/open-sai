"""Sai MCP server: annotations, exposure, tool calls, errors, and secrets."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from sai_cli.core import keeper, storage, stream
from sai_cli.mcp_server import server, tools

ADDRESS = "0x" + "b" * 40
ENDPOINT = keeper.MAINNET_HTTP
TOKEN = "header.secret-session-token-value.signature"

TRADE_TOOLS = {
    "trade_open",
    "trade_close",
    "trade_tpsl",
    "trade_leverage",
    "trade_margin",
    "trade_partial_close",
}
READ_TOOLS = {
    "mcp_info",
    "auth_status",
    "market_list",
    "market_prices",
    "account_positions",
    "account_history",
    "account_balances",
    "intent_local",
    "intent_status",
    "preview_open",
    "preview_close",
    "preview_tpsl",
    "preview_leverage",
    "preview_margin",
    "preview_partial_close",
}
LOCAL_TOOLS = {"mcp_info", "auth_status", "intent_local"}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    for name in (
        "SAI_ENDPOINT",
        "SAI_WS_ENDPOINT",
        "SAI_LCD_ENDPOINT",
        server.ENV_MODE,
    ):
        monkeypatch.delenv(name, raising=False)


def stored_session(hours: float = 1, endpoint: str = ENDPOINT) -> dict[str, str]:
    return {
        "token": TOKEN,
        "evmAddress": ADDRESS,
        "endpoint": endpoint,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
    }


def text_of(content: Any) -> str:
    block = content[0]
    assert isinstance(block, TextContent)
    return block.text


def invoke(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call a tool through FastMCP and return its decoded JSON text."""
    result = asyncio.run(mcp.call_tool(name, arguments or {}))
    text = text_of(result[0] if isinstance(result, tuple) else result)
    assert TOKEN not in text
    return json.loads(text)


class FakeKeeper:
    """Answer keeper.request by query and record calls and submissions."""

    def __init__(self, answers: dict[str, Any] | None = None, lose_ack=False):
        self.answers = answers or {}
        self.lose_ack = lose_ack
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.submitted: list[dict[str, Any]] = []

    def __call__(
        self,
        endpoint: str,
        query: str,
        variables: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((query, variables))
        if query == keeper.SUBMIT:
            assert token == TOKEN and variables is not None
            envelope = variables["input"]
            self.submitted.append(envelope)
            if self.lose_ack:
                raise keeper.KeeperError(
                    "Keeper request failed or returned invalid JSON"
                )
            return {
                "recordUserIntent": {
                    "intentKey": envelope["intentKey"],
                    "lifecycleStep": "jwt_pass",
                    "execActor": "server",
                    "evmAddress": ADDRESS,
                }
            }
        return json.loads(json.dumps(self.answers[query]))

    def intent(self) -> dict[str, Any]:
        """Return the last submitted intent without the envelope fields."""
        body = dict(self.submitted[-1]["intent"])
        body.pop("intentSchemaVersion")
        body.pop("tsIntentInit")
        return body


@pytest.fixture
def mcp() -> FastMCP:
    return server.create_server()


# Schema and exposure


def test_every_tool_has_a_description_and_the_right_annotations(mcp):
    registered = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(registered) == READ_TOOLS | TRADE_TOOLS
    for name, tool in registered.items():
        hints = tool.annotations
        assert tool.description, name
        assert hints is not None and hints.title, name
        if name in TRADE_TOOLS:
            assert hints.readOnlyHint is False, name
            assert hints.destructiveHint is True, name
            assert hints.idempotentHint is False, name
            assert hints.openWorldHint is True, name
        else:
            assert hints.readOnlyHint is True, name
            assert hints.destructiveHint is False, name
            assert hints.idempotentHint is True, name
            assert hints.openWorldHint is (name not in LOCAL_TOOLS), name


def test_no_tool_accepts_a_token_or_passphrase(mcp):
    for tool in mcp._tool_manager.list_tools():
        for field in tool.parameters.get("properties", {}):
            assert "token" not in field and "pass" not in field, tool.name


def test_read_only_mode_removes_exactly_the_trade_tools(mcp):
    removed = server.apply_exposed_tools_mode(mcp, "read-only")
    assert set(removed) == TRADE_TOOLS
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == READ_TOOLS
    assert invoke(mcp, "mcp_info")["exposedTools"] == "read-only"


def test_all_mode_keeps_every_tool(mcp):
    assert server.apply_exposed_tools_mode(mcp, "all") == []
    assert invoke(mcp, "mcp_info")["exposedTools"] == "all"


def test_mode_defaults_to_read_only_and_rejects_unknown_values(monkeypatch):
    assert server.exposed_tools_mode() == "read-only"
    monkeypatch.setenv(server.ENV_MODE, " ALL ")
    assert server.exposed_tools_mode() == "all"
    monkeypatch.setenv(server.ENV_MODE, "write")
    with pytest.raises(SystemExit, match="Invalid SAI_MCP_EXPOSED_TOOLS 'write'"):
        server.exposed_tools_mode()


def test_config_reads_endpoints_from_the_environment(monkeypatch):
    config = tools.load_config()
    assert config.endpoint == ENDPOINT
    assert config.ws_endpoint == keeper.MAINNET_WS
    monkeypatch.setenv("SAI_ENDPOINT", "http://127.0.0.1:9/query")
    monkeypatch.setenv("SAI_LCD_ENDPOINT", "http://127.0.0.1:8")
    config = tools.load_config()
    assert config.ws_endpoint == "ws://127.0.0.1:9/query"
    assert config.lcd_endpoint == "http://127.0.0.1:8"
    monkeypatch.setenv("SAI_WS_ENDPOINT", "ws://127.0.0.1:7/query")
    assert tools.load_config().ws_endpoint == "ws://127.0.0.1:7/query"


# Reads


def test_market_prices_returns_keeper_data(mcp, monkeypatch):
    prices = {"oracle": {"tokenPricesUsd": [{"priceUsd": 1.0}]}}
    fake = FakeKeeper({keeper.PRICES: prices})
    monkeypatch.setattr(keeper, "request", fake)
    assert invoke(mcp, "market_prices") == prices


def test_account_positions_defaults_to_the_session_address(mcp, monkeypatch):
    fake = FakeKeeper({keeper.POSITIONS: {"perp": {"trades": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    error = invoke(mcp, "account_positions")
    assert error["code"] == "SESSION_UNAVAILABLE"
    assert "Pass address" in error["error"]
    assert fake.calls == []

    storage.save_session(stored_session())
    assert invoke(mcp, "account_positions") == {"perp": {"trades": []}}
    assert fake.calls[-1] == (keeper.POSITIONS, {"trader": ADDRESS})
    other = "0x" + "c" * 40
    invoke(mcp, "account_positions", {"address": other})
    assert fake.calls[-1] == (keeper.POSITIONS, {"trader": other})


def test_account_balances_reads_one_snapshot(mcp, monkeypatch):
    seen: list[tuple[str, dict[str, Any]]] = []

    def snapshot(ws: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert query == stream.BALANCES
        seen.append((ws, variables))
        return {"data": {"userBalances": []}}

    monkeypatch.setattr(stream, "snapshot", snapshot)
    result = invoke(mcp, "account_balances", {"address": ADDRESS})
    assert result == {"data": {"userBalances": []}}
    assert seen == [(keeper.MAINNET_WS, {"user": ADDRESS})]


# Previews and trades


OPEN_ARGS = {
    "market_id": 0,
    "side": "long",
    "order_type": "limit",
    "amount": "10",
    "leverage": "2",
    "slippage": "1",
    "price": "100",
    "take_profit": "120",
}


def test_preview_open_is_exactly_what_trade_open_submits(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", OPEN_ARGS)
    assert preview["dryRun"] is True
    assert fake.submitted == []

    result = invoke(mcp, "trade_open", OPEN_ARGS)
    assert result["status"] == "accepted"
    assert "not yet confirmed" in result["message"]
    assert fake.intent() == preview["intent"]
    assert fake.submitted[-1]["intentKey"] == result["intentKey"]


def test_market_preview_needs_no_session_and_validation_comes_first(mcp):
    preview = invoke(
        mcp, "preview_open", {**OPEN_ARGS, "order_type": "market", "price": None}
    )
    assert preview["requiresMarketPriceRefresh"] is True
    error = invoke(mcp, "trade_open", {**OPEN_ARGS, "amount": "-1"})
    assert error["code"] == "INVALID_REQUEST"
    assert "Amount" in error["error"]


def test_lost_ack_reports_unknown_then_intent_status_reconciles(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper(lose_ack=True)
    monkeypatch.setattr(keeper, "request", fake)
    error = invoke(mcp, "trade_close", {"trade_id": 7})
    key = fake.submitted[-1]["intentKey"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    assert error["intentKey"] == key
    assert "intent_status" in error["next"]
    assert invoke(mcp, "intent_local") == [
        {"intentKey": key, "kind": "CLOSE_POSITION", "status": "unknown"}
    ]

    blocked = invoke(mcp, "trade_close", {"trade_id": 8})
    assert blocked["code"] == "INVALID_REQUEST"
    assert key in blocked["error"]

    row = {
        "intentKey": key,
        "lifecycleStep": "exec_pass",
        "execActor": "server",
        "evmAddress": ADDRESS,
    }
    trade = {"intentKey": key, "trade": {"id": 7}}
    fake.answers = {
        keeper.INTENTS: {"userIntents": [row]},
        keeper.HISTORY: {"perp": {"tradeHistory": [trade]}},
    }
    status = invoke(mcp, "intent_status", {"key": key})
    assert status["matchedTradeHistory"] == [trade]
    assert fake.calls[-2] == (keeper.INTENTS, {"user": ADDRESS, "key": key})
    assert invoke(mcp, "intent_local")[0]["status"] == "terminal"


def test_file_backend_session_is_refused_without_a_passphrase(mcp, monkeypatch):
    storage.save_session(stored_session(), "passphrase")
    fake = FakeKeeper({keeper.POSITIONS: {"perp": {"trades": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    for name, arguments in (
        ("preview_margin", {"trade_id": 1, "direction": "add", "amount": "5"}),
        ("preview_partial_close", {"trade_id": 1, "percent": 50}),
        ("trade_close", {"trade_id": 1}),
        ("auth_status", {}),
    ):
        error = invoke(mcp, name, arguments)
        assert error["code"] == "SESSION_UNAVAILABLE", name
        assert "keyring backend file" in error["error"]
        assert "sai auth login" in error["error"]
    assert invoke(mcp, "account_positions")["code"] == "SESSION_UNAVAILABLE"
    assert invoke(mcp, "account_positions", {"address": ADDRESS}) == {
        "perp": {"trades": []}
    }
    info = invoke(mcp, "mcp_info")["session"]
    assert info["backend"] == "file" and info["usable"] is False
    assert fake.submitted == []


def test_expired_or_missing_session_is_reported(mcp):
    assert invoke(mcp, "auth_status")["code"] == "SESSION_UNAVAILABLE"
    storage.save_session(stored_session(hours=-1))
    error = invoke(mcp, "auth_status")
    assert error == {
        "error": "Trading session expired; sign in again",
        "code": "SESSION_UNAVAILABLE",
    }
    session = invoke(mcp, "mcp_info")["session"]
    assert session["expired"] is True and session["usable"] is False


# Errors and secrets


def test_keeper_errors_map_to_stable_codes(mcp, monkeypatch):
    cases = (
        (keeper.AdmissionRejected("rejected"), "ADMISSION_REJECTED"),
        (keeper.NotSent("not sent"), "NOT_SENT"),
        (keeper.KeeperError("failed"), "KEEPER_ERROR"),
        (stream.StreamError("ended"), "STREAM_ERROR"),
        (KeyError("perp"), "INCOMPLETE_DATA"),
    )
    for exc, code in cases:

        def fail(*_args: Any, exc: Exception = exc, **_kwargs: Any) -> Any:
            raise exc

        monkeypatch.setattr(keeper, "request", fail)
        assert invoke(mcp, "market_list")["code"] == code


def test_unexpected_errors_are_generic_and_logged(mcp, monkeypatch, caplog):
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(keeper, "request", fail)
    with caplog.at_level("ERROR", logger="sai_cli.mcp_server"):
        result = invoke(mcp, "market_list")
    assert result == {"error": tools.INTERNAL, "code": "INTERNAL_ERROR"}
    assert "RuntimeError" in caplog.text


def test_session_reports_never_include_the_token(mcp):
    storage.save_session(stored_session())
    status = invoke(mcp, "auth_status")
    assert set(status) == {"address", "expiresAt", "endpoint", "backend"}
    assert (status["address"], status["endpoint"]) == (ADDRESS, ENDPOINT)
    info = invoke(mcp, "mcp_info")
    assert info["session"]["address"] == ADDRESS
    assert info["session"]["usable"] is True
    assert info["provenance"]["kind"] in ("source-checkout", "installed")
    assert info["endpoints"]["keeper"] == ENDPOINT


# Stdio


def test_stdio_server_lists_tools(tmp_path):
    script = Path(sys.executable).with_name("sai-mcp")
    command = str(script) if script.exists() else sys.executable
    args = [] if script.exists() else ["-m", "sai_cli.mcp_server.server"]

    async def listing(mode: str, errlog: TextIO) -> tuple[set[str], dict[str, Any]]:
        params = StdioServerParameters(
            command=command,
            args=args,
            env={"SAI_HOME": str(tmp_path), server.ENV_MODE: mode},
        )
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            listed = await client.list_tools()
            called = await client.call_tool("mcp_info", {})
        return {tool.name for tool in listed.tools}, json.loads(text_of(called.content))

    with open(tmp_path / "stderr.log", "w") as errlog:
        names, info = asyncio.run(listing("read-only", errlog))
        assert names == READ_TOOLS
        assert info["exposedTools"] == "read-only"
        names, _ = asyncio.run(listing("all", errlog))
        assert names == READ_TOOLS | TRADE_TOOLS
