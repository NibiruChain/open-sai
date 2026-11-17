"""Sai MCP server over stdio.

Tools return JSON strings and never raise. Read-only tools carry
`readOnlyHint=True`; trade tools carry `destructiveHint=True`. Exposure goes
by name, not annotation: the six TRADE_TOOL_NAMES are registered only when
SAI_MCP_EXPOSED_TOOLS is `all`. The default, `read-only`, applies to the
module-level `mcp` too, so a server started without the variable never
offers a trade tool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from functools import partial
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import info, tools

ENV_MODE = "SAI_MCP_EXPOSED_TOOLS"
MODES = ("read-only", "all")
TRADE_TOOL_NAMES = frozenset(
    {
        "trade_open",
        "trade_close",
        "trade_tpsl",
        "trade_leverage",
        "trade_margin",
        "trade_partial_close",
    }
)

INSTRUCTIONS = """\
Sai perpetuals trading on Nibiru through Sai Keeper.

- Call mcp_info first to learn the version, endpoints, exposure mode, and \
whether a usable session exists.
- Before any trade, call the matching preview_* tool, show the user the \
preview, and get their explicit confirmation. Only then call the trade_* \
tool with the same arguments.
- "accepted" means Keeper admitted the intent. It does not mean the trade \
executed. Use intent_status with the intent key to follow it.
- If a trade returns code OUTCOME_UNKNOWN, call intent_status with its \
intentKey. Never resubmit blindly; the first request may have gone through.
- Never ask the user for a session token or passphrase. They sign in \
themselves by running `sai auth login` or `sai auth import` in a terminal.
- Never read MCP client config files to learn about this server; call \
mcp_info instead.
"""

Side = Literal["long", "short"]
OrderType = Literal["market", "limit", "stop"]
Direction = Literal["add", "remove"]


def read_only(title: str, *, open_world: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=open_world,
    )


def destructive(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )


async def call(action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> str:
    """Run a blocking tool implementation off the event loop."""
    config = tools.load_config()
    return await asyncio.to_thread(tools.run, partial(action, config, *args, **kwargs))


# Reads


async def auth_status() -> str:
    """Show the signed-in account and session expiry, never the token.

    The session comes from `sai auth login` or `sai auth import`, run by the
    user in a terminal with the default test keyring backend.
    """
    return await call(tools.auth_status)


async def market_list() -> str:
    """List Keeper borrowing markets: market id, price, open state, leverage
    limits, minimum position size, fees, and tokens."""
    return await call(tools.market_list)


async def market_prices() -> str:
    """List current indexed USD token prices."""
    return await call(tools.market_prices)


async def account_positions(address: str | None = None) -> str:
    """List open positions and pending orders.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account.
    """
    return await call(tools.account_positions, address)


async def account_history(address: str | None = None) -> str:
    """Read the latest 50 trade history rows.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account.
    """
    return await call(tools.account_history, address)


async def account_balances(address: str | None = None) -> str:
    """Read one balance snapshot, including Sai deposits and credits.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account.
    """
    return await call(tools.account_balances, address)


async def intent_local(address: str | None = None) -> str:
    """List intents this machine recorded, with their local status.

    Status `unknown` blocks new trades until intent_status reconciles it.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account.
    """
    return await call(tools.intent_local, address)


async def intent_status(address: str | None = None, key: str | None = None) -> str:
    """Read Keeper's latest intent records; with KEY, reconcile that intent.

    Reconciling updates only the local record: a server failure or matched
    trade history is final, a server acceptance is accepted, and a key
    Keeper never recorded past its admission window stops blocking trades.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account.
        key: Intent key from a trade result or an OUTCOME_UNKNOWN error.
    """
    return await call(tools.intent_status, address, key)


# Previews and trades share one implementation per action.


async def preview_open(
    market_id: int,
    side: Side,
    order_type: OrderType,
    amount: str,
    leverage: str,
    slippage: str,
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
) -> str:
    """Preview opening a market, limit, or stop trade without submitting.

    A market order preview sets requiresMarketPriceRefresh: the submit uses
    Keeper's current price.

    Args:
        market_id: Market id from market_list.
        side: long or short.
        order_type: market, limit, or stop.
        amount: Collateral in USD as a decimal string, at most 6 decimals.
        leverage: Leverage as a decimal string, such as "5".
        slippage: Maximum slippage percentage as a decimal string.
        price: Trigger price, required for limit and stop. A market order
            submits Keeper's current price instead.
        take_profit: Optional take-profit price.
        stop_loss: Optional stop-loss price.
    """
    return await call(
        tools.open_trade,
        dry_run=True,
        market_id=market_id,
        side=side,
        order_type=order_type,
        amount=amount,
        leverage=leverage,
        slippage=slippage,
        price=price,
        take_profit=take_profit,
        stop_loss=stop_loss,
    )


async def trade_open(
    market_id: int,
    side: Side,
    order_type: OrderType,
    amount: str,
    leverage: str,
    slippage: str,
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
) -> str:
    """Submit an open-trade intent. Call preview_open and get the user's
    explicit confirmation first. `accepted` means Keeper admitted the intent,
    not that it executed.

    Args:
        market_id: Market id from market_list.
        side: long or short.
        order_type: market, limit, or stop.
        amount: Collateral in USD as a decimal string, at most 6 decimals.
        leverage: Leverage as a decimal string, such as "5".
        slippage: Maximum slippage percentage as a decimal string.
        price: Trigger price, required for limit and stop. A market order
            submits Keeper's current price instead.
        take_profit: Optional take-profit price.
        stop_loss: Optional stop-loss price.
    """
    return await call(
        tools.open_trade,
        dry_run=False,
        market_id=market_id,
        side=side,
        order_type=order_type,
        amount=amount,
        leverage=leverage,
        slippage=slippage,
        price=price,
        take_profit=take_profit,
        stop_loss=stop_loss,
    )


async def preview_close(trade_id: int, cancel_order: bool = False) -> str:
    """Preview closing a position or cancelling a pending order.

    Args:
        trade_id: Trade id from account_positions.
        cancel_order: True to cancel a pending order instead of closing.
    """
    return await call(
        tools.close_trade, dry_run=True, trade_id=trade_id, cancel_order=cancel_order
    )


async def trade_close(trade_id: int, cancel_order: bool = False) -> str:
    """Submit a close or cancel intent. Preview and confirm first.

    Args:
        trade_id: Trade id from account_positions.
        cancel_order: True to cancel a pending order instead of closing.
    """
    return await call(
        tools.close_trade, dry_run=False, trade_id=trade_id, cancel_order=cancel_order
    )


async def preview_tpsl(
    trade_id: int,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    clear_take_profit: bool = False,
    clear_stop_loss: bool = False,
) -> str:
    """Preview setting or clearing take-profit and stop-loss prices.

    Args:
        trade_id: Trade id from account_positions.
        take_profit: New take-profit price as a decimal string.
        stop_loss: New stop-loss price as a decimal string.
        clear_take_profit: True to remove the take-profit.
        clear_stop_loss: True to remove the stop-loss.
    """
    return await call(
        tools.set_tpsl,
        dry_run=True,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
    )


async def trade_tpsl(
    trade_id: int,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    clear_take_profit: bool = False,
    clear_stop_loss: bool = False,
) -> str:
    """Submit a take-profit and stop-loss change. Preview and confirm first.

    Args:
        trade_id: Trade id from account_positions.
        take_profit: New take-profit price as a decimal string.
        stop_loss: New stop-loss price as a decimal string.
        clear_take_profit: True to remove the take-profit.
        clear_stop_loss: True to remove the stop-loss.
    """
    return await call(
        tools.set_tpsl,
        dry_run=False,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
    )


async def preview_leverage(trade_id: int, leverage: str) -> str:
    """Preview setting target leverage without changing TP or SL.

    Args:
        trade_id: Trade id from account_positions.
        leverage: Target leverage as a decimal string.
    """
    return await call(
        tools.set_leverage, dry_run=True, trade_id=trade_id, leverage=leverage
    )


async def trade_leverage(trade_id: int, leverage: str) -> str:
    """Submit a target-leverage change. Preview and confirm first.

    Args:
        trade_id: Trade id from account_positions.
        leverage: Target leverage as a decimal string.
    """
    return await call(
        tools.set_leverage, dry_run=False, trade_id=trade_id, leverage=leverage
    )


async def preview_margin(trade_id: int, direction: Direction, amount: str) -> str:
    """Quote adding or removing margin from exact contract state.

    The change is a target-leverage intent: the amount is a quote, not a
    cap, and the actual change may differ if the position moves first.
    Needs a signed-in session to read the position.

    Args:
        trade_id: Trade id from account_positions.
        direction: add or remove.
        amount: USDC amount to quote, as a decimal string.
    """
    return await call(
        tools.change_margin,
        dry_run=True,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
    )


async def trade_margin(trade_id: int, direction: Direction, amount: str) -> str:
    """Submit a margin change. Preview and confirm first.

    Refuses if the position changed since a fresh quote. The amount is a
    quote, not a cap.

    Args:
        trade_id: Trade id from account_positions.
        direction: add or remove.
        amount: USDC amount to quote, as a decimal string.
    """
    return await call(
        tools.change_margin,
        dry_run=False,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
    )


async def preview_partial_close(trade_id: int, percent: int) -> str:
    """Preview reducing an open USDC position by a whole percentage.

    Sized from contract state and checked against the market minimum.
    Needs a signed-in session to read the position.

    Args:
        trade_id: Trade id from account_positions.
        percent: Whole percentage to close, 1 to 99.
    """
    return await call(
        tools.partial_close, dry_run=True, trade_id=trade_id, percent=percent
    )


async def trade_partial_close(trade_id: int, percent: int) -> str:
    """Submit a partial close. Preview and confirm first.

    Refuses if the position changed since the reduction was sized.

    Args:
        trade_id: Trade id from account_positions.
        percent: Whole percentage to close, 1 to 99.
    """
    return await call(
        tools.partial_close, dry_run=False, trade_id=trade_id, percent=percent
    )


# Registration

READ_TOOLS: tuple[tuple[Callable[..., Any], ToolAnnotations], ...] = (
    (auth_status, read_only("Auth Status", open_world=False)),
    (market_list, read_only("Market List")),
    (market_prices, read_only("Market Prices")),
    (account_positions, read_only("Account Positions")),
    (account_history, read_only("Account History")),
    (account_balances, read_only("Account Balances")),
    (intent_local, read_only("Local Intents", open_world=False)),
    (intent_status, read_only("Intent Status")),
    (preview_open, read_only("Preview Open Trade")),
    (preview_close, read_only("Preview Close Trade")),
    (preview_tpsl, read_only("Preview TP/SL")),
    (preview_leverage, read_only("Preview Leverage")),
    (preview_margin, read_only("Preview Margin Change")),
    (preview_partial_close, read_only("Preview Partial Close")),
)

TRADE_TOOLS: tuple[tuple[Callable[..., Any], ToolAnnotations], ...] = (
    (trade_open, destructive("Open Trade")),
    (trade_close, destructive("Close Trade")),
    (trade_tpsl, destructive("Set TP/SL")),
    (trade_leverage, destructive("Set Leverage")),
    (trade_margin, destructive("Change Margin")),
    (trade_partial_close, destructive("Partial Close")),
)


def create_server(mode: str = "read-only") -> FastMCP:
    """Build a server for exposure MODE.

    Trade tools are registered only when MODE is `all`. The check goes by
    name, so a read tool that is mislabeled cannot carry a trade through.
    """
    mode = exposed_tools_mode(mode)
    server = FastMCP("sai", instructions=INSTRUCTIONS)

    async def mcp_info() -> str:
        """Report this server's version, provenance, endpoints, exposed tools,
        and session state. Call this first.

        Never read MCP client config files to learn about this server; they
        can hold secrets. The report never includes the session token.
        """

        def report(config: tools.Config) -> dict[str, Any]:
            try:
                ws: str | None = config.ws_endpoint
            except ValueError:
                ws = None
            names = sorted(tool.name for tool in server._tool_manager.list_tools())
            return {
                "name": "sai",
                "package": info.PACKAGE,
                "version": info.version(),
                "mcpVersion": info.version("mcp"),
                "provenance": info.provenance(),
                "endpoints": {
                    "keeper": config.endpoint,
                    "websocket": ws,
                    "lcd": config.lcd_endpoint,
                },
                "exposedTools": mode,
                "tools": names,
                "session": tools.session_state(config),
            }

        return await call(report)

    server.add_tool(mcp_info, annotations=read_only("MCP Info", open_world=False))
    selected = READ_TOOLS + (TRADE_TOOLS if mode == "all" else ())
    for function, hints in selected:
        name = getattr(function, "__name__", "")
        if mode != "all" and (not name or name in TRADE_TOOL_NAMES):
            raise RuntimeError(f"Trade tool {name!r} is in READ_TOOLS")
        server.add_tool(function, annotations=hints)
    return server


def exposed_tools_mode(value: str | None = None) -> str:
    """Return the exposure mode from VALUE or SAI_MCP_EXPOSED_TOOLS.

    Exits with a message on an unknown mode.
    """
    raw = os.environ.get(ENV_MODE, "read-only") if value is None else value
    mode = raw.strip().lower()
    if mode not in MODES:
        raise SystemExit(
            f"Invalid {ENV_MODE} {raw!r}. Expected one of: {', '.join(MODES)}."
        )
    return mode


# Built at import from SAI_MCP_EXPOSED_TOOLS, so `mcp` is read-only unless
# the variable says `all`, however the server is started.
mcp = create_server(exposed_tools_mode())


def main() -> None:
    """Serve over stdio with the exposure mode chosen at import."""
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
