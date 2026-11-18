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
import json
import logging
import os
import sys
from collections.abc import Callable
from functools import partial
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core import settings as sai_settings
from ..core.settings import Settings
from . import info, tools

log = logging.getLogger("sai_cli.mcp_server")

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
preview and the account it names, and get their explicit confirmation. Only then call the trade_* \
tool with the same arguments plus the preview's previewDigest, and for a \
market open also its referencePrice.
- If a trade_* tool returns PREVIEW_MISMATCH or PRICE_MOVED, nothing was \
submitted: show the user the new preview in the result, get confirmation \
again, and retry with the new previewDigest (and referencePrice).
- "accepted" means Keeper admitted the intent. It does not mean the trade \
executed. Use intent_status with the intent key to follow it.
- If a trade returns code OUTCOME_UNKNOWN, call intent_status with its \
intentKey. Never resubmit blindly; the first request may have gone through.
- After a timeout or error on any trade_* call, call intent_local before \
retrying. trade_open refuses with RECENT_OPEN_PENDING while a recent open \
is unknown or accepted; check those intents with intent_status, and pass \
allow_concurrent_open=true only if the user wants another open position.
- The user can connect several accounts; one is active. auth_list shows \
them. Preview and trade tools act on the active account unless given \
address, which must be a connected account. Every preview and trade names \
its account; tell the user which account before they confirm.
- Call auth_use only when the user asks to switch accounts. It changes the \
active account for the user's terminal too.
- Account reads report the address they read. With addressSource \
"session", the address came from the active account; say so to the user, \
and pass address explicitly to read any other account.
- Never ask the user for a session token. They sign in \
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


# Resolved once, so endpoints cannot change mid-session. `main` resolves at
# start; `create_server` clears it, and the first tool call resolves it then.
_settings: Settings | None = None


def server_settings() -> Settings:
    """Settings from the environment, then $SAI_HOME/config.toml, then
    mainnet defaults, resolved on first use and kept."""
    global _settings
    if _settings is None:
        _settings = sai_settings.load(warn=log.warning)
    return _settings


async def call(action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> str:
    """Run a blocking tool implementation off the event loop."""
    try:
        config = server_settings()
    except sai_settings.SettingsError as exc:
        return json.dumps(tools.error(str(exc), "BAD_SETTINGS"))
    return await asyncio.to_thread(tools.run, partial(action, config, *args, **kwargs))


# Reads


async def auth_status(address: str | None = None) -> str:
    """Show the active account and its session expiry, never the token.

    Sessions come from `sai auth login` or `sai auth import`, run by the
    user in a terminal.

    Args:
        address: A connected account to check instead of the active one.
    """
    return await call(tools.auth_status, address)


async def auth_list() -> str:
    """List connected accounts: address, expiry, whether each is usable on
    this server's endpoint, and which one is active. Never shows tokens."""
    return await call(tools.auth_list)


async def auth_use(address: str) -> str:
    """Make a connected account the active one. Call only when the user
    asks to switch; the CLI shares the active account. Returns auth_list.

    Args:
        address: EVM address (0x...) of a connected account from auth_list.
    """
    return await call(tools.auth_use, address)


async def market_list() -> str:
    """List Keeper borrowing markets: market id, price, open state, leverage
    limits, minimum position size, fees, and tokens."""
    return await call(tools.market_list)


async def market_prices() -> str:
    """List current indexed USD token prices."""
    return await call(tools.market_prices)


async def account_positions(address: str | None = None) -> str:
    """List open positions and pending orders.

    The result names the `address` read and its `addressSource`.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.account_positions, address)


async def account_history(address: str | None = None) -> str:
    """Read the latest 50 trade history rows.

    The result names the `address` read and its `addressSource`.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.account_history, address)


async def account_balances(address: str | None = None) -> str:
    """Read one balance snapshot, including Sai deposits and credits.

    The result names the `address` read and its `addressSource`.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.account_balances, address)


async def intent_local(address: str | None = None) -> str:
    """List intents this machine recorded, with their local status.

    Status `unknown` blocks new trades until intent_status reconciles it.
    The result names the `address` read and its `addressSource`.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
    """
    return await call(tools.intent_local, address)


async def intent_status(address: str | None = None, key: str | None = None) -> str:
    """Read Keeper's latest intent records; with KEY, reconcile that intent.

    With KEY this also writes: it updates this machine's local intent cache,
    the record intent_local lists and trades check. It never changes
    anything on Keeper. Reconciling updates only the local record: a server failure or matched
    trade history is final, a server acceptance is accepted, and a key
    Keeper never recorded past its admission window stops blocking trades.

    Args:
        address: EVM address (0x...). Defaults to the signed-in account,
            unless its session is for another Keeper endpoint.
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
    address: str | None = None,
) -> str:
    """Preview opening a market, limit, or stop trade without submitting.

    Show the user the result and get confirmation; trade_open then needs its
    previewDigest. A market order preview sets requiresMarketPriceRefresh
    and reports Keeper's current price as referencePrice: the submit uses
    the price current at that time, and trade_open needs referencePrice too.

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
        address: Connected account to act as (0x...). Defaults to the
            active account.
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
        address=address,
    )


async def trade_open(
    market_id: int,
    side: Side,
    order_type: OrderType,
    amount: str,
    leverage: str,
    slippage: str,
    preview_digest: str,
    price: str | None = None,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    reference_price: str | None = None,
    allow_concurrent_open: bool = False,
    address: str | None = None,
) -> str:
    """Submit an open-trade intent. Call preview_open with the same
    arguments, show the user the preview, and get their explicit
    confirmation first. `accepted` means Keeper admitted the intent, not
    that it executed.

    Refuses without submitting: PREVIEW_MISMATCH when the intent differs
    from the confirmed preview, and, for a market order, PRICE_MOVED when
    Keeper's current price moved from reference_price by more than the
    slippage percent. Both return a new preview to show the user. Also
    RECENT_OPEN_PENDING when an open for this account from the last few
    minutes is still unknown or accepted: call intent_local and
    intent_status before retrying.

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
        preview_digest: previewDigest from the confirmed preview_open.
        reference_price: referencePrice from the confirmed preview_open;
            required for market orders only.
        allow_concurrent_open: True to submit despite a recent pending
            open, only after intent_status shows it and the user confirms
            they want another position. An intent still `unknown` blocks
            every trade until intent_status reconciles it.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.open_trade,
        dry_run=False,
        preview_digest=preview_digest,
        reference_price=reference_price,
        allow_concurrent_open=allow_concurrent_open,
        market_id=market_id,
        side=side,
        order_type=order_type,
        amount=amount,
        leverage=leverage,
        slippage=slippage,
        price=price,
        take_profit=take_profit,
        stop_loss=stop_loss,
        address=address,
    )


async def preview_close(
    trade_id: int, cancel_order: bool = False, address: str | None = None
) -> str:
    """Preview closing a position or cancelling a pending order. Show the
    user the result; trade_close needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        cancel_order: True to cancel a pending order instead of closing.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.close_trade,
        dry_run=True,
        trade_id=trade_id,
        cancel_order=cancel_order,
        address=address,
    )


async def trade_close(
    trade_id: int,
    preview_digest: str,
    cancel_order: bool = False,
    address: str | None = None,
) -> str:
    """Submit a close or cancel intent. Call preview_close, show the user the
    preview, and get confirmation first. Refuses with PREVIEW_MISMATCH and a
    new preview if the intent differs from the confirmed one.

    Args:
        trade_id: Trade id from account_positions.
        preview_digest: previewDigest from the confirmed preview_close.
        cancel_order: True to cancel a pending order instead of closing.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.close_trade,
        dry_run=False,
        trade_id=trade_id,
        cancel_order=cancel_order,
        preview_digest=preview_digest,
        address=address,
    )


async def preview_tpsl(
    trade_id: int,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    clear_take_profit: bool = False,
    clear_stop_loss: bool = False,
    address: str | None = None,
) -> str:
    """Preview setting or clearing take-profit and stop-loss prices. Show the
    user the result; trade_tpsl needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        take_profit: New take-profit price as a decimal string.
        stop_loss: New stop-loss price as a decimal string.
        clear_take_profit: True to remove the take-profit.
        clear_stop_loss: True to remove the stop-loss.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_tpsl,
        dry_run=True,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        address=address,
    )


async def trade_tpsl(
    trade_id: int,
    preview_digest: str,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    clear_take_profit: bool = False,
    clear_stop_loss: bool = False,
    address: str | None = None,
) -> str:
    """Submit a take-profit and stop-loss change. Call preview_tpsl, show the
    user the preview, and get confirmation first. Refuses with
    PREVIEW_MISMATCH and a new preview if the intent differs.

    Args:
        trade_id: Trade id from account_positions.
        preview_digest: previewDigest from the confirmed preview_tpsl.
        take_profit: New take-profit price as a decimal string.
        stop_loss: New stop-loss price as a decimal string.
        clear_take_profit: True to remove the take-profit.
        clear_stop_loss: True to remove the stop-loss.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_tpsl,
        dry_run=False,
        preview_digest=preview_digest,
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        address=address,
    )


async def preview_leverage(
    trade_id: int, leverage: str, address: str | None = None
) -> str:
    """Preview setting target leverage without changing TP or SL. Show the
    user the result; trade_leverage needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        leverage: Target leverage as a decimal string.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_leverage,
        dry_run=True,
        trade_id=trade_id,
        leverage=leverage,
        address=address,
    )


async def trade_leverage(
    trade_id: int, leverage: str, preview_digest: str, address: str | None = None
) -> str:
    """Submit a target-leverage change. Call preview_leverage, show the user
    the preview, and get confirmation first. Refuses with PREVIEW_MISMATCH
    and a new preview if the intent differs.

    Args:
        trade_id: Trade id from account_positions.
        leverage: Target leverage as a decimal string.
        preview_digest: previewDigest from the confirmed preview_leverage.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.set_leverage,
        dry_run=False,
        trade_id=trade_id,
        leverage=leverage,
        preview_digest=preview_digest,
        address=address,
    )


async def preview_margin(
    trade_id: int, direction: Direction, amount: str, address: str | None = None
) -> str:
    """Quote adding or removing margin from exact contract state.

    The change is a target-leverage intent: the amount is a quote, not a
    cap, and the actual change may differ if the position moves first.
    Needs a signed-in session to read the position. Show the user the quote;
    trade_margin needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        direction: add or remove.
        amount: USDC amount to quote, as a decimal string.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.change_margin,
        dry_run=True,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        address=address,
    )


async def trade_margin(
    trade_id: int,
    direction: Direction,
    amount: str,
    preview_digest: str,
    address: str | None = None,
) -> str:
    """Submit a margin change. Call preview_margin, show the user the quote,
    and get confirmation first.

    The digest covers the intent and the quote, so it refuses with
    PREVIEW_MISMATCH and a new preview if the position changed since the
    confirmed quote. The amount is a quote, not a cap.

    Args:
        trade_id: Trade id from account_positions.
        direction: add or remove.
        amount: USDC amount to quote, as a decimal string.
        preview_digest: previewDigest from the confirmed preview_margin.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.change_margin,
        dry_run=False,
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        preview_digest=preview_digest,
        address=address,
    )


async def preview_partial_close(
    trade_id: int, percent: int, address: str | None = None
) -> str:
    """Preview reducing an open USDC position by a whole percentage.

    Sized from contract state and checked against the market minimum.
    Needs a signed-in session to read the position. Show the user the
    result; trade_partial_close needs its previewDigest.

    Args:
        trade_id: Trade id from account_positions.
        percent: Whole percentage to close, 1 to 99.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.partial_close,
        dry_run=True,
        trade_id=trade_id,
        percent=percent,
        address=address,
    )


async def trade_partial_close(
    trade_id: int, percent: int, preview_digest: str, address: str | None = None
) -> str:
    """Submit a partial close. Call preview_partial_close, show the user the
    preview, and get confirmation first.

    Refuses with PREVIEW_MISMATCH and a new preview if the position changed
    since the confirmed reduction was sized.

    Args:
        trade_id: Trade id from account_positions.
        percent: Whole percentage to close, 1 to 99.
        preview_digest: previewDigest from the confirmed preview_partial_close.
        address: Connected account to act as (0x...). Defaults to the
            active account.
    """
    return await call(
        tools.partial_close,
        dry_run=False,
        trade_id=trade_id,
        percent=percent,
        preview_digest=preview_digest,
        address=address,
    )


# Registration

READ_TOOLS: tuple[tuple[Callable[..., Any], ToolAnnotations], ...] = (
    (auth_status, read_only("Auth Status", open_world=False)),
    (auth_list, read_only("Connected Accounts", open_world=False)),
    (
        auth_use,
        ToolAnnotations(
            title="Switch Account",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    ),
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


def create_server(mode: str = "read-only", settings: Settings | None = None) -> FastMCP:
    """Build a server for exposure MODE.

    Trade tools are registered only when MODE is `all`. The check goes by
    name, so a read tool that is mislabeled cannot carry a trade through.
    SETTINGS, when None, resolve on the first tool call.
    """
    global _settings
    mode = exposed_tools_mode(mode)
    _settings = settings
    server = FastMCP("sai", instructions=INSTRUCTIONS)

    async def mcp_info() -> str:
        """Report this server's version, provenance, endpoints, exposed tools,
        and session state. Call this first.

        Never read MCP client config files to learn about this server; they
        can hold secrets. The report never includes the session token.
        """

        def report(config: Settings) -> dict[str, Any]:
            names = sorted(tool.name for tool in server._tool_manager.list_tools())
            return {
                "name": "sai",
                "package": info.PACKAGE,
                "version": info.version(),
                "mcpVersion": info.version("mcp"),
                "provenance": info.provenance(),
                "endpoints": {
                    "keeper": sai_settings.public_url(config.endpoint),
                    "websocket": sai_settings.public_url(config.ws_endpoint),
                    "lcd": sai_settings.public_url(config.lcd_endpoint),
                },
                "configFile": str(sai_settings.config_path(os.environ)),
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
    """Serve over stdio with the exposure mode chosen at import and settings
    resolved once, at start."""
    global _settings
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        _settings = sai_settings.load(warn=log.warning)
    except sai_settings.SettingsError as exc:
        raise SystemExit(f"sai-mcp: {exc}") from None
    mcp.run()


if __name__ == "__main__":
    main()
