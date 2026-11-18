"""Sai MCP server: annotations, exposure, tool calls, errors, and secrets."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from sai_cli.core import keeper, margin, storage, stream
from sai_cli.mcp_server import info as sai_info
from sai_cli.mcp_server import server, tools

ADDRESS = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
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
    "auth_list",
    "auth_use",
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
LOCAL_TOOLS = {"mcp_info", "auth_status", "auth_list", "auth_use", "intent_local"}
# Changes only which connected account is active on this machine.
SWITCH_TOOLS = {"auth_use"}


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
    return server.create_server("all")


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
        elif name in SWITCH_TOOLS:
            assert hints.readOnlyHint is False, name
            assert hints.destructiveHint is False, name
            assert hints.idempotentHint is True, name
            assert hints.openWorldHint is False, name
        else:
            assert hints.readOnlyHint is True, name
            assert hints.destructiveHint is False, name
            assert hints.idempotentHint is True, name
            assert hints.openWorldHint is (name not in LOCAL_TOOLS), name


def test_no_tool_accepts_a_token_or_passphrase(mcp):
    for tool in mcp._tool_manager.list_tools():
        for field in tool.parameters.get("properties", {}):
            assert "token" not in field and "pass" not in field, tool.name


def names(mcp: FastMCP) -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()}


def test_read_only_mode_registers_no_trade_tool():
    assert server.TRADE_TOOL_NAMES == TRADE_TOOLS
    for mcp in (server.create_server(), server.create_server("read-only")):
        assert names(mcp) == READ_TOOLS
        assert invoke(mcp, "mcp_info")["exposedTools"] == "read-only"


def test_all_mode_registers_every_tool(mcp):
    assert names(mcp) == READ_TOOLS | TRADE_TOOLS
    assert invoke(mcp, "mcp_info")["exposedTools"] == "all"


def test_read_only_mode_goes_by_name_not_annotation(monkeypatch):
    """A trade tool mislabeled read-only is still never registered."""
    mislabeled = ((server.trade_close, server.read_only("Close Trade")),)
    monkeypatch.setattr(server, "READ_TOOLS", server.READ_TOOLS + mislabeled)
    with pytest.raises(RuntimeError, match="trade_close"):
        server.create_server("read-only")


def test_invalid_mode_exits():
    with pytest.raises(SystemExit, match="Invalid SAI_MCP_EXPOSED_TOOLS"):
        server.create_server("write")


def test_module_level_server_is_read_only_without_the_variable(tmp_path):
    """Importing `server.mcp` and running it, as `mcp run` does, never
    exposes trades unless SAI_MCP_EXPOSED_TOOLS is `all`."""
    code = (
        "import json; from sai_cli.mcp_server import server;"
        " print(json.dumps(sorted(t.name for t in"
        " server.mcp._tool_manager.list_tools())))"
    )
    for env, expected in (({}, READ_TOOLS), ({server.ENV_MODE: "all"}, None)):
        done = subprocess.run(
            [sys.executable, "-c", code],
            env={"SAI_HOME": str(tmp_path), "PATH": os.defpath, **env},
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        listed = set(json.loads(done.stdout))
        assert listed == (expected or READ_TOOLS | TRADE_TOOLS)


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
    assert invoke(mcp, "account_positions") == {
        "address": ADDRESS,
        "addressSource": "session",
        "sessionExpired": False,
        "endpointMatches": True,
        "perp": {"trades": []},
    }
    assert fake.calls[-1] == (keeper.POSITIONS, {"trader": ADDRESS})
    other = "0x" + "c" * 40
    assert invoke(mcp, "account_positions", {"address": other}) == {
        "address": other,
        "addressSource": "argument",
        "perp": {"trades": []},
    }
    assert fake.calls[-1] == (keeper.POSITIONS, {"trader": other})


def test_expired_session_still_supplies_the_address_flagged(mcp, monkeypatch):
    storage.save_session(stored_session(hours=-1))
    fake = FakeKeeper({keeper.HISTORY: {"perp": {"tradeHistory": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    result = invoke(mcp, "account_history")
    assert result["address"] == ADDRESS
    assert result["addressSource"] == "session"
    assert result["sessionExpired"] is True


@pytest.mark.parametrize(
    "name",
    [
        "account_positions",
        "account_history",
        "account_balances",
        "intent_local",
        "intent_status",
    ],
)
def test_session_for_another_endpoint_is_refused_for_the_default_address(
    mcp, monkeypatch, name
):
    storage.save_session(stored_session(endpoint="http://127.0.0.1:1/query"))
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    monkeypatch.setattr(stream, "snapshot", fake)
    error = invoke(mcp, name)
    assert error["code"] == "SESSION_UNAVAILABLE"
    assert "Pass address explicitly" in error["error"]
    assert fake.calls == []


def test_account_balances_reads_one_snapshot(mcp, monkeypatch):
    seen: list[tuple[str, dict[str, Any]]] = []

    def snapshot(ws: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert query == stream.BALANCES
        seen.append((ws, variables))
        return {"data": {"userBalances": []}}

    monkeypatch.setattr(stream, "snapshot", snapshot)
    result = invoke(mcp, "account_balances", {"address": ADDRESS})
    assert result == {
        "address": ADDRESS,
        "addressSource": "argument",
        "data": {"userBalances": []},
    }
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


def markets(price: float = 100.5) -> dict[str, Any]:
    return {
        "perp": {
            "borrowings": [
                {
                    "marketId": 0,
                    "price": price,
                    "isOpen": True,
                    "minPositionSizeUSD": 1,
                    "collateralToken": {"id": 1, "symbol": "USDC"},
                }
            ]
        }
    }


def snapshots(monkeypatch, *collaterals: str) -> None:
    """Make margin.snapshot return one position state per call, in order."""
    states = iter(
        (
            {"collateral_amount": amount, "leverage": "2"},
            {"available_to_trade": "10000000"},
            {"marketId": 0, "minLeverage": 1.1, "maxLeverage": 100},
        )
        for amount in collaterals
    )
    monkeypatch.setattr(margin, "snapshot", lambda *args: next(states))


def confirm(
    mcp: FastMCP, preview_tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Preview, and return the trade arguments that bind to the preview."""
    preview = invoke(mcp, preview_tool, arguments)
    assert preview["dryRun"] is True
    return {**arguments, "preview_digest": preview["previewDigest"]}


def test_preview_open_is_exactly_what_trade_open_submits(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", OPEN_ARGS)
    assert preview["dryRun"] is True
    assert preview["previewDigest"] == tools.preview_digest(preview)
    assert fake.submitted == []

    result = invoke(
        mcp, "trade_open", {**OPEN_ARGS, "preview_digest": preview["previewDigest"]}
    )
    assert result["status"] == "accepted"
    assert "not yet confirmed" in result["message"]
    assert fake.intent() == preview["intent"]
    assert fake.submitted[-1]["intentKey"] == result["intentKey"]


def test_digest_binds_account_and_reference_not_market_intent_price():
    def market(intent_price: str, reference: str, account: str) -> dict:
        return {
            "account": account,
            "referencePrice": reference,
            "intent": {
                "kind": "OPEN_TRADE",
                "body": {"orderType": "market", "intentPrice": intent_price},
            },
        }

    base = tools.preview_digest(market("5", "100", ADDRESS))
    # The refreshed intent price, reference spelling, and address case don't
    # change what the user confirmed.
    assert tools.preview_digest(market("6", "100.0", ADDRESS.upper())) == base
    assert tools.preview_digest(market("5", "101", ADDRESS)) != base
    assert tools.preview_digest(market("5", "100", OTHER)) != base
    limit = {"intent": {"kind": "OPEN_TRADE", "body": {"orderType": "limit"}}}
    assert tools.preview_digest(limit) != tools.preview_digest(
        {
            "intent": {
                "kind": "OPEN_TRADE",
                "body": {"orderType": "limit", "intentPrice": "5"},
            }
        }
    )
    assert len(tools.preview_digest(limit)) == 64


def test_trades_require_the_preview_digest(mcp):
    for name in TRADE_TOOLS:
        tool = mcp._tool_manager.get_tool(name)
        assert tool is not None
        assert "preview_digest" in tool.parameters["required"], name
    error = invoke(mcp, "trade_close", {"trade_id": 1, "preview_digest": ""})
    assert error["code"] == "INVALID_REQUEST"
    assert "previewDigest" in error["error"]


PREVIEWS = (
    ("preview_open", OPEN_ARGS),
    ("preview_open", {**OPEN_ARGS, "order_type": "market", "price": None}),
    ("preview_close", {"trade_id": 7}),
    ("preview_close", {"trade_id": 7, "cancel_order": True}),
    ("preview_tpsl", {"trade_id": 7, "take_profit": "120"}),
    ("preview_leverage", {"trade_id": 7, "leverage": "3"}),
    ("preview_margin", {"trade_id": 7, "direction": "add", "amount": "1"}),
    ("preview_partial_close", {"trade_id": 7, "percent": 50}),
)


@pytest.mark.parametrize(("name", "arguments"), PREVIEWS)
def test_previews_never_submit(mcp, monkeypatch, name, arguments):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    snapshots(monkeypatch, "4000000")
    preview = invoke(mcp, name, arguments)
    assert preview["dryRun"] is True
    assert len(preview["previewDigest"]) == 64
    assert all(query != keeper.SUBMIT for query, _ in fake.calls)
    assert storage.list_pending(ENDPOINT, ADDRESS) == []


@pytest.mark.parametrize(
    ("name", "preview_args", "trade_args"),
    [
        ("close", {"trade_id": 7}, {"trade_id": 8}),
        ("close", {"trade_id": 7}, {"trade_id": 7, "cancel_order": True}),
        (
            "leverage",
            {"trade_id": 7, "leverage": "3"},
            {"trade_id": 7, "leverage": "4"},
        ),
        (
            "tpsl",
            {"trade_id": 7, "take_profit": "120"},
            {"trade_id": 7, "take_profit": "121"},
        ),
        ("open", OPEN_ARGS, {**OPEN_ARGS, "amount": "11"}),
    ],
)
def test_trade_that_differs_from_its_preview_is_refused(
    mcp, monkeypatch, name, preview_args, trade_args
):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    digest = confirm(mcp, f"preview_{name}", preview_args)["preview_digest"]
    error = invoke(mcp, f"trade_{name}", {**trade_args, "preview_digest": digest})
    assert error["code"] == "PREVIEW_MISMATCH"
    assert "confirmation again" in error["error"]
    fresh = invoke(mcp, f"preview_{name}", trade_args)
    assert error["preview"] == fresh
    assert fake.submitted == []
    assert storage.list_pending(ENDPOINT, ADDRESS) == []


MARKET_ARGS = {**OPEN_ARGS, "order_type": "market", "price": None}


def test_market_open_binds_to_the_reference_price(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets(price=100)})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_ARGS)
    assert preview["requiresMarketPriceRefresh"] is True
    assert preview["referencePrice"] == "100"
    trade = {
        **MARKET_ARGS,
        "preview_digest": preview["previewDigest"],
        "reference_price": preview["referencePrice"],
    }

    missing = invoke(mcp, "trade_open", {**trade, "reference_price": None})
    assert missing["code"] == "INVALID_REQUEST"
    assert "referencePrice" in missing["error"]

    # Slippage is 1%: a 1.5% move is refused with a fresh preview.
    fake.answers[keeper.MARKETS] = markets(price=101.5)
    moved = invoke(mcp, "trade_open", trade)
    assert moved["code"] == "PRICE_MOVED"
    assert "1.5000%" in moved["error"]
    assert moved["preview"]["referencePrice"] == "101.5"
    assert moved["preview"]["previewDigest"] != preview["previewDigest"]
    assert fake.submitted == []

    # Retrying the old confirmation with the new price needs a new digest.
    retry = invoke(mcp, "trade_open", {**trade, "reference_price": "101.5"})
    assert retry["code"] == "PREVIEW_MISMATCH"
    assert retry["preview"]["previewDigest"] == moved["preview"]["previewDigest"]
    assert fake.submitted == []

    # A 0.5% move is within slippage and submits the refreshed price.
    fake.answers[keeper.MARKETS] = markets(price=100.5)
    result = invoke(mcp, "trade_open", trade)
    assert result["status"] == "accepted"
    assert result["referencePrice"] == "100.5"
    assert fake.intent()["body"]["intentPrice"] == "100.5"


def test_market_open_resubmits_only_with_the_new_confirmation(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets(price=100)})
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_open", MARKET_ARGS)
    fake.answers[keeper.MARKETS] = markets(price=110)
    moved = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_ARGS,
            "preview_digest": preview["previewDigest"],
            "reference_price": preview["referencePrice"],
        },
    )
    assert moved["code"] == "PRICE_MOVED"
    fresh = moved["preview"]
    result = invoke(
        mcp,
        "trade_open",
        {
            **MARKET_ARGS,
            "preview_digest": fresh["previewDigest"],
            "reference_price": fresh["referencePrice"],
        },
    )
    assert result["status"] == "accepted"
    assert fake.intent()["body"]["intentPrice"] == "110"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("close", {"trade_id": 7}),
        ("leverage", {"trade_id": 7, "leverage": "3"}),
        ("tpsl", {"trade_id": 7, "take_profit": "120"}),
        ("open", OPEN_ARGS),
    ],
)
def test_preview_is_bound_to_the_signed_in_account(mcp, monkeypatch, name, arguments):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, f"preview_{name}", arguments)
    assert preview["account"] == ADDRESS
    # The person signs in to another account before confirming.
    storage.save_session({**stored_session(), "evmAddress": OTHER})
    error = invoke(
        mcp,
        f"trade_{name}",
        {**arguments, "preview_digest": preview["previewDigest"]},
    )
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["account"] == OTHER
    assert fake.submitted == []
    assert storage.list_pending(ENDPOINT, OTHER) == []


OTHER_TOKEN = "header.other-account-session-token.signature"


def connect_two_accounts() -> None:
    """Connect ADDRESS, then OTHER, which becomes active."""
    storage.save_session(stored_session())
    storage.save_session(
        {**stored_session(), "evmAddress": OTHER, "token": OTHER_TOKEN}
    )


def test_auth_list_and_auth_use_switch_accounts(mcp):
    connect_two_accounts()
    listed = invoke(mcp, "auth_list")
    assert OTHER_TOKEN not in json.dumps(listed)
    assert {r["address"]: r["active"] for r in listed["accounts"]} == {
        ADDRESS: False,
        OTHER: True,
    }
    assert all(r["usable"] for r in listed["accounts"])
    assert invoke(mcp, "auth_status")["address"] == OTHER
    assert invoke(mcp, "auth_status", {"address": ADDRESS})["address"] == ADDRESS

    switched = invoke(mcp, "auth_use", {"address": ADDRESS.upper().replace("0X", "0x")})
    assert {r["address"]: r["active"] for r in switched["accounts"]} == {
        ADDRESS: True,
        OTHER: False,
    }
    assert storage.load_session()["evmAddress"] == ADDRESS
    info = invoke(mcp, "mcp_info")["session"]
    assert (info["address"], info["connectedAccounts"]) == (ADDRESS, 2)

    missing = invoke(mcp, "auth_use", {"address": "0x" + "d" * 40})
    assert missing["code"] == "INVALID_REQUEST"
    assert "is not connected" in missing["error"]


def test_trade_acts_as_the_requested_connected_account(mcp, monkeypatch):
    connect_two_accounts()
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    args = {"trade_id": 7, "address": ADDRESS}
    preview = invoke(mcp, "preview_close", args)
    assert preview["account"] == ADDRESS
    result = invoke(
        mcp, "trade_close", {**args, "preview_digest": preview["previewDigest"]}
    )
    # FakeKeeper checks that the submission carries ADDRESS's token, not the
    # active account's.
    assert result["status"] == "accepted", result
    assert result["account"] == ADDRESS
    assert storage.load_session()["evmAddress"] == OTHER


def test_switching_accounts_after_a_preview_refuses_the_trade(mcp, monkeypatch):
    connect_two_accounts()
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    preview = invoke(mcp, "preview_close", {"trade_id": 7})
    assert preview["account"] == OTHER
    invoke(mcp, "auth_use", {"address": ADDRESS})
    error = invoke(
        mcp,
        "trade_close",
        {"trade_id": 7, "preview_digest": preview["previewDigest"]},
    )
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["account"] == ADDRESS
    assert fake.submitted == []


def test_preview_for_an_unconnected_account_fails(mcp, monkeypatch):
    connect_two_accounts()
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    error = invoke(mcp, "preview_close", {"trade_id": 7, "address": "0x" + "d" * 40})
    assert error["code"] == "SESSION_UNAVAILABLE"
    assert "is not connected" in error["error"]


def test_errors_redact_every_connected_token():
    connect_two_accounts()
    text = f"failed with {TOKEN} and {OTHER_TOKEN}"
    assert tools.redact(text) == f"failed with {tools.REDACTED} and {tools.REDACTED}"


def test_preview_without_a_session_names_no_account(mcp, monkeypatch):
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    preview = invoke(mcp, "preview_close", {"trade_id": 7})
    assert preview["account"] is None


def test_reference_price_is_only_for_market_orders(mcp, monkeypatch):
    storage.save_session(stored_session())
    monkeypatch.setattr(keeper, "request", FakeKeeper())
    trade = confirm(mcp, "preview_open", OPEN_ARGS)
    error = invoke(mcp, "trade_open", {**trade, "reference_price": "100"})
    assert error["code"] == "INVALID_REQUEST"


def test_margin_trade_binds_to_the_confirmed_quote(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    args = {"trade_id": 7, "direction": "add", "amount": "1"}
    snapshots(monkeypatch, "2000000")
    preview = invoke(mcp, "preview_margin", args)
    trade = {**args, "preview_digest": preview["previewDigest"]}

    # The position changed after the user confirmed: a fresh, consistent
    # pair of snapshots still differs from the confirmed quote.
    snapshots(monkeypatch, "2500000", "2500000")
    error = invoke(mcp, "trade_margin", trade)
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["quote"]["collateralBefore"] == "2500000"
    assert fake.submitted == []

    snapshots(monkeypatch, "2000000", "2000000")
    result = invoke(mcp, "trade_margin", trade)
    assert result["status"] == "accepted"
    assert result["quote"] == preview["quote"]
    assert fake.intent() == preview["intent"]


def test_partial_close_trade_binds_to_the_confirmed_size(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper({keeper.MARKETS: markets()})
    monkeypatch.setattr(keeper, "request", fake)
    args = {"trade_id": 7, "percent": 50}
    snapshots(monkeypatch, "4000000")
    preview = invoke(mcp, "preview_partial_close", args)
    assert preview["intent"]["body"]["collateral"]["amountUsdc"] == "2000000"
    trade = {**args, "preview_digest": preview["previewDigest"]}

    snapshots(monkeypatch, "3000000", "3000000")
    error = invoke(mcp, "trade_partial_close", trade)
    assert error["code"] == "PREVIEW_MISMATCH"
    assert error["preview"]["intent"]["body"]["collateral"]["amountUsdc"] == "1500000"
    assert fake.submitted == []

    snapshots(monkeypatch, "4000000", "4000000")
    result = invoke(mcp, "trade_partial_close", trade)
    assert result["status"] == "accepted"
    assert fake.intent() == preview["intent"]


def test_recent_pending_open_blocks_a_second_open(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper()
    monkeypatch.setattr(keeper, "request", fake)
    first = invoke(mcp, "trade_open", confirm(mcp, "preview_open", OPEN_ARGS))
    assert first["status"] == "accepted"

    other = {**OPEN_ARGS, "side": "short"}
    trade = confirm(mcp, "preview_open", other)
    blocked = invoke(mcp, "trade_open", trade)
    assert blocked["code"] == "RECENT_OPEN_PENDING"
    assert blocked["intentKeys"] == [first["intentKey"]]
    assert "intent_local" in blocked["error"]
    assert len(fake.submitted) == 1

    # A close is not an open and is not blocked.
    invoke(mcp, "trade_close", confirm(mcp, "preview_close", {"trade_id": 7}))
    assert len(fake.submitted) == 2

    allowed = invoke(mcp, "trade_open", {**trade, "allow_concurrent_open": True})
    assert allowed["status"] == "accepted"
    assert len(fake.submitted) == 3


def test_unknown_open_after_a_timeout_blocks_with_its_key(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper(lose_ack=True)
    monkeypatch.setattr(keeper, "request", fake)
    trade = confirm(mcp, "preview_open", OPEN_ARGS)
    lost = invoke(mcp, "trade_open", trade)
    assert lost["code"] == "OUTCOME_UNKNOWN"
    fake.lose_ack = False
    blocked = invoke(mcp, "trade_open", trade)
    assert blocked["code"] == "RECENT_OPEN_PENDING"
    assert blocked["intentKeys"] == [lost["intentKey"]]
    assert len(fake.submitted) == 1


def test_only_recent_unresolved_opens_count():
    key = "20260101T000000000Z_AAAAAAAAAAAAAAAA"
    storage.pending(key, ENDPOINT, ADDRESS, "OPEN_TRADE", "")
    storage.set_status(key, ENDPOINT, ADDRESS, "accepted")
    assert tools.recent_opens(ENDPOINT, ADDRESS) == [
        {"intentKey": key, "status": "accepted"}
    ]
    later = datetime.now(UTC).timestamp() + tools.RECENT_OPEN_SECONDS + 1
    assert tools.recent_opens(ENDPOINT, ADDRESS, now=later) == []
    assert tools.recent_opens("http://127.0.0.1:1/query", ADDRESS) == []
    storage.set_status(key, ENDPOINT, ADDRESS, "terminal")
    assert tools.recent_opens(ENDPOINT, ADDRESS) == []


def test_validation_comes_before_the_session(mcp):
    error = invoke(
        mcp, "trade_open", {**OPEN_ARGS, "amount": "-1", "preview_digest": "0" * 64}
    )
    assert error["code"] == "INVALID_REQUEST"
    assert "Amount" in error["error"]


def test_lost_ack_reports_unknown_then_intent_status_reconciles(mcp, monkeypatch):
    storage.save_session(stored_session())
    fake = FakeKeeper(lose_ack=True)
    monkeypatch.setattr(keeper, "request", fake)
    error = invoke(mcp, "trade_close", confirm(mcp, "preview_close", {"trade_id": 7}))
    key = fake.submitted[-1]["intentKey"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    assert error["intentKey"] == key
    assert "intent_status" in error["next"]
    local = invoke(mcp, "intent_local")
    assert local["address"] == ADDRESS and local["addressSource"] == "session"
    assert local["intents"] == [
        {"intentKey": key, "kind": "CLOSE_POSITION", "status": "unknown"}
    ]

    blocked = invoke(mcp, "trade_close", confirm(mcp, "preview_close", {"trade_id": 8}))
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
    assert status["address"] == ADDRESS
    assert invoke(mcp, "intent_local")["intents"][0]["status"] == "terminal"


def test_passphrase_session_is_refused_without_a_prompt(mcp, monkeypatch):
    # A session from an older version that encrypted it with a passphrase.
    path = storage.directory() / "session.json"
    path.write_text(json.dumps({"version": 1, "nonce": "", "ciphertext": ""}))
    fake = FakeKeeper({keeper.POSITIONS: {"perp": {"trades": []}}})
    monkeypatch.setattr(keeper, "request", fake)
    for name, arguments in (
        ("preview_margin", {"trade_id": 1, "direction": "add", "amount": "5"}),
        ("preview_partial_close", {"trade_id": 1, "percent": 50}),
        ("trade_close", {"trade_id": 1, "preview_digest": "0" * 64}),
        ("auth_status", {}),
    ):
        error = invoke(mcp, name, arguments)
        assert error["code"] == "SESSION_UNAVAILABLE", name
        assert "passphrase-encrypted" in error["error"]
        assert "sai auth login" in error["error"]
    assert invoke(mcp, "account_positions")["code"] == "SESSION_UNAVAILABLE"
    assert invoke(mcp, "account_positions", {"address": ADDRESS})["perp"] == {
        "trades": []
    }
    info = invoke(mcp, "mcp_info")["session"]
    assert info["signedIn"] is False and "passphrase-encrypted" in info["error"]
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


def test_logged_and_returned_errors_redact_the_session_token(mcp, monkeypatch, caplog):
    storage.save_session(stored_session())

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(keeper, "request", fail)
    with caplog.at_level("ERROR", logger="sai_cli.mcp_server"):
        assert invoke(mcp, "market_list")["code"] == "INTERNAL_ERROR"
    assert "RuntimeError: boom" in caplog.text
    assert tools.REDACTED in caplog.text
    assert TOKEN not in caplog.text

    def known(*_args: Any, **_kwargs: Any) -> Any:
        raise keeper.KeeperError(f"failed {TOKEN}")

    monkeypatch.setattr(keeper, "request", known)
    error = invoke(mcp, "market_list")
    assert error["error"] == f"failed {tools.REDACTED}"


def test_mcp_info_strips_credentials_from_endpoints(monkeypatch):
    monkeypatch.setenv("SAI_ENDPOINT", "https://user:pw@k.example:8443/query?key=s#f")
    monkeypatch.setenv("SAI_LCD_ENDPOINT", "https://lcd.example/?apikey=s")
    storage.save_session(stored_session(endpoint="https://u:p@k.example/q?t=s"))
    info = invoke(server.create_server(), "mcp_info")
    assert info["endpoints"] == {
        "keeper": "https://k.example:8443/query",
        "websocket": "wss://k.example:8443/query",
        "lcd": "https://lcd.example/",
    }
    assert info["session"]["endpoint"] == "https://k.example/q"
    text = json.dumps(info)
    for secret in ("user:pw", "key=", "apikey", "u:p@", "t=s"):
        assert secret not in text
    assert sai_info.public_url("http://[::1]:9/q?x=1") == "http://[::1]:9/q"


def test_session_reports_never_include_the_token(mcp):
    storage.save_session(stored_session())
    status = invoke(mcp, "auth_status")
    assert set(status) == {"address", "expiresAt", "endpoint"}
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
