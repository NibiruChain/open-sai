"""Sai trader CLI with Keeper queries, intents, and live feeds."""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from importlib import metadata, resources
from typing import Any
from urllib.parse import urlsplit

import click
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect
from websockets.typing import Subprotocol

from .core import clipboard, intents, keeper, margin, storage
from .utils.repl_skin import ReplSkin

PACKAGE = "sai-cli"


def package_version() -> str:
    """Return the installed package version; pyproject.toml sets it."""
    return metadata.version(PACKAGE)


def skill_bytes() -> bytes:
    """Return the agent skill packaged with this version of the CLI."""
    return resources.files("sai_cli").joinpath("skills/SKILL.md").read_bytes()


def print_skill(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(skill_bytes(), nl=False)
    ctx.exit()


def emit(ctx: click.Context, value: Any) -> None:
    click.echo(json.dumps(value, indent=None if ctx.obj["json"] else 2, default=str))


def read_passphrase(ctx: click.Context, *, new: bool = False) -> str:
    """Read the backend `file` passphrase from --passphrase-fd or a prompt."""
    fd = ctx.obj.get("passphrase_fd")
    if fd is not None:
        return os.read(fd, 4096).decode().rstrip("\r\n")
    return click.prompt(
        "New session passphrase" if new else "Session passphrase",
        hide_input=True,
        confirmation_prompt=new,
        err=True,
    )


def save(ctx: click.Context, result: dict[str, str]) -> None:
    """Store RESULT with the selected keyring backend."""
    if ctx.obj["keyring_backend"] == "file":
        storage.save_session(result, read_passphrase(ctx, new=True))
    else:
        storage.save_session(result)


def session(ctx: click.Context) -> dict[str, str]:
    # Check the stored backend first so a mismatch fails before any prompt.
    stored = storage.stored_backend()
    if ctx.obj["keyring_backend"] == "file" and stored == "test":
        raise ValueError(
            "Stored session uses keyring backend test; drop --keyring-backend"
            " file or sign in again"
        )
    if ctx.obj["keyring_backend"] == "file" and stored == "file":
        result = storage.load_session(read_passphrase(ctx))
    else:
        result = storage.load_session()
    if result.get("endpoint") != ctx.obj["endpoint"]:
        raise ValueError("Stored session belongs to another Keeper endpoint")
    expiry = datetime.fromisoformat(result["expiresAt"])
    if expiry <= datetime.now(UTC):
        raise ValueError("Trading session expired; sign in again")
    return result


def import_session(token: str, endpoint: str) -> dict[str, str]:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        address = data["sub"]
        expires = datetime.fromtimestamp(data["exp"], UTC)
        if (
            not isinstance(address, str)
            or not address.startswith("0x")
            or len(address) != 42
            or data.get("purpose") != "user_intent"
            or expires <= datetime.now(UTC)
        ):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid or expired Sai session token") from exc
    return {
        "token": token,
        "evmAddress": address,
        "expiresAt": expires.isoformat(),
        "endpoint": endpoint,
    }


@click.group(invoke_without_command=True)
@click.version_option(package_name=PACKAGE, prog_name="sai")
@click.option(
    "--skill",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=print_skill,
    help="Print the agent skill file and exit.",
)
@click.option(
    "--endpoint",
    default=keeper.MAINNET_HTTP,
    show_default=True,
    help="Keeper GraphQL HTTP endpoint.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Compact machine-readable JSON."
)
@click.option(
    "--keyring-backend",
    type=click.Choice(storage.BACKENDS),
    default="test",
    show_default=True,
    envvar="SAI_KEYRING_BACKEND",
    help="Session storage. test: encrypted with a built-in key, no prompt."
    " file: encrypted with your passphrase.",
)
@click.option(
    "--passphrase-fd",
    type=click.IntRange(0),
    help="Keyring backend file: read the passphrase from an inherited file descriptor.",
)
@click.pass_context
def main(
    ctx: click.Context,
    endpoint: str,
    json_output: bool,
    keyring_backend: str,
    passphrase_fd: int | None,
) -> None:
    """Inspect Sai markets and account data, and submit trading intents."""
    ctx.ensure_object(dict)
    if passphrase_fd is not None and keyring_backend != "file":
        # Scripts written before keyring backends pass only --passphrase-fd.
        source = ctx.get_parameter_source("keyring_backend")
        if source is not click.core.ParameterSource.DEFAULT:
            raise click.UsageError("--passphrase-fd requires --keyring-backend file")
        keyring_backend = "file"
    ctx.obj.update(
        endpoint=endpoint,
        json=json_output,
        keyring_backend=keyring_backend,
        passphrase_fd=passphrase_fd,
    )
    if ctx.invoked_subcommand is None:
        repl(ctx)


def repl_args(line: str) -> list[str]:
    """Turn one REPL line into CLI arguments; `help [COMMAND...]` shows help."""
    args = shlex.split(line)
    if args and args[0] in ("help", "?"):
        return [*args[1:], "--help"]
    return args


def repl(ctx: click.Context, pt_input: Any = None, pt_output: Any = None) -> None:
    """Run the interactive shell.

    History is kept in $SAI_HOME/trader/history when stdin is a terminal or a
    prompt_toolkit input is supplied. Ctrl-C cancels the current line or
    command and returns to the prompt; Ctrl-D or `exit` leaves.
    """
    skin = ReplSkin("sai", version=package_version())
    skin.print_banner()
    click.echo("Enter a command, 'help', or 'exit'.")
    pt_session = (
        skin.create_prompt_session(input=pt_input, output=pt_output)
        if pt_input is not None or sys.stdin.isatty()
        else None
    )
    while True:
        try:
            line = skin.get_input(pt_session)
        except KeyboardInterrupt:
            continue
        except EOFError:
            click.echo()
            break
        if line in ("exit", "quit"):
            break
        try:
            args = repl_args(line)
            if args:
                main.main(
                    args=[
                        "--endpoint",
                        ctx.obj["endpoint"],
                        *(["--json"] if ctx.obj["json"] else []),
                        "--keyring-backend",
                        ctx.obj["keyring_backend"],
                        *args,
                    ],
                    prog_name="sai",
                    standalone_mode=False,
                )
        except (click.Abort, KeyboardInterrupt):
            click.echo("Interrupted", err=True)
        except (click.ClickException, ValueError, keeper.KeeperError) as exc:
            click.echo(f"Error: {exc}", err=True)


@main.group()
def auth() -> None:
    """Connect and manage a local encrypted trading session."""


@auth.command("login")
@click.option(
    "--account",
    help="Foundry keystore name (~/.foundry/keystores). cast prompts for its password.",
)
@click.option(
    "--address",
    help="EVM address of the account. With --account, defaults to"
    " `cast wallet address`, which prompts for the password a second time.",
)
@click.option(
    "--wallet",
    help="Experimental: wallet name in the local ud wallet manager.",
)
@click.pass_context
def login(
    ctx: click.Context, account: str | None, address: str | None, wallet: str | None
) -> None:
    """Sign Keeper's trading-session challenge with a local key.

    Use --account for a Foundry keystore. The CLI checks that the challenge
    is a Sai `UserTradingSession` for this address before cast signs it.
    """
    if bool(account) == bool(wallet):
        raise click.UsageError("Pass exactly one of --account or --wallet")
    if wallet and not address:
        raise click.UsageError("--wallet requires --address")
    try:
        if account:
            address = address or keeper.cast_address(account)
            result = keeper.cast_session(ctx.obj["endpoint"], address, account)
        else:
            assert wallet and address
            result = keeper.wallet_session(ctx.obj["endpoint"], address, wallet)
        result["endpoint"] = ctx.obj["endpoint"]
        save(ctx, result)
        emit(
            ctx,
            {
                "address": result["evmAddress"],
                "expiresAt": result["expiresAt"],
                "endpoint": result["endpoint"],
            },
        )
    except (ValueError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


NO_CLIPBOARD = (
    "No terminal and no working clipboard command ({names}). Ask the person"
    " to run `sai auth import` in their own terminal."
)


def read_token() -> str:
    """Read the token from a hidden prompt or, without a terminal, the
    clipboard. Never from arguments, the environment, or piped stdin."""
    if sys.stdin.isatty():
        token = click.prompt(
            "Paste the Sai session token (Enter reads the clipboard)",
            hide_input=True,
            default="",
            show_default=False,
            err=True,
        ).strip()
        if token:
            return token
    copied = clipboard.read()
    if copied is None:
        if sys.stdin.isatty():
            raise ValueError(f"No working clipboard command ({clipboard.NAMES})")
        raise ValueError(NO_CLIPBOARD.format(names=clipboard.NAMES))
    if not copied[0]:
        raise ValueError(
            "Clipboard is empty; copy the session token from Sai Web and retry"
        )
    return copied[0]


@auth.command("import")
@click.pass_context
def import_token(ctx: click.Context) -> None:
    """Connect with a session token copied from Sai Web.

    In a terminal, paste the token at the hidden prompt or press Enter to read
    the clipboard. Without a terminal, as when an agent runs this, read the
    clipboard. After storing the token, clear the clipboard if it still holds
    the token. Prints the address, expiry, and endpoint, never the token.
    """
    try:
        token = read_token()
        result = import_session(token, ctx.obj["endpoint"])
        save(ctx, result)
        # Re-read: with backend file, the person may have copied a passphrase
        # since, and that must survive.
        copied = clipboard.read()
        if copied is not None and copied[0] == token and not clipboard.clear(copied[1]):
            click.echo("Warning: could not clear the clipboard", err=True)
        emit(
            ctx,
            {
                "address": result["evmAddress"],
                "expiresAt": result["expiresAt"],
                "endpoint": result["endpoint"],
            },
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show the account and expiration without showing the token."""
    try:
        result = session(ctx)
        emit(
            ctx,
            {
                "address": result["evmAddress"],
                "expiresAt": result["expiresAt"],
                "endpoint": result["endpoint"],
            },
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def market() -> None:
    """Inspect markets and prices."""


@market.command("list")
@click.pass_context
def market_list(ctx: click.Context) -> None:
    """List Keeper borrowing markets."""
    perform_query(ctx, keeper.MARKETS)


@market.command("prices")
@click.pass_context
def market_prices(ctx: click.Context) -> None:
    """List current indexed prices."""
    perform_query(ctx, keeper.PRICES)


@main.group()
def account() -> None:
    """Inspect positions and account activity."""


def perform_query(
    ctx: click.Context, query: str, variables: dict[str, Any] | None = None
) -> None:
    try:
        emit(ctx, keeper.request(ctx.obj["endpoint"], query, variables))
    except keeper.KeeperError as exc:
        raise click.ClickException(str(exc)) from exc


@account.command("positions")
@click.argument("address")
@click.pass_context
def positions(ctx: click.Context, address: str) -> None:
    """List positions and pending orders for ADDRESS."""
    perform_query(ctx, keeper.POSITIONS, {"trader": address})


@account.command("history")
@click.argument("address")
@click.pass_context
def history(ctx: click.Context, address: str) -> None:
    """Read trade history for ADDRESS."""
    perform_query(ctx, keeper.HISTORY, {"trader": address})


@account.command("balances")
@click.argument("address")
@click.option(
    "--ws-endpoint", help="Override the WebSocket endpoint derived from --endpoint."
)
@click.pass_context
def balances(ctx: click.Context, address: str, ws_endpoint: str | None) -> None:
    """Read one account balance snapshot from Keeper's subscription."""
    query = "subscription($user:String!) { userBalances(where:{user:$user}) { amount usd_value token_info { name symbol decimals type } } }"
    watch_stream(
        ws_endpoint or derive_ws(ctx.obj["endpoint"]),
        query,
        {"user": address},
        once=True,
        ctx=ctx,
    )


@main.group()
def intent() -> None:
    """Inspect and reconcile trading request outcomes."""


@intent.command("status")
@click.argument("address")
@click.option("--key", help="Specific intent key.")
@click.pass_context
def intent_status(ctx: click.Context, address: str, key: str | None) -> None:
    """Read Keeper's latest intent records; reconcile a known key."""
    try:
        data = keeper.request(
            ctx.obj["endpoint"], keeper.INTENTS, {"user": address, "key": key}
        )
        if key and data.get("userIntents"):
            row = data["userIntents"][0]
            step = row.get("lifecycleStep")
            if (
                row.get("intentKey") == key
                and str(row.get("evmAddress") or "").lower() == address.lower()
            ):
                if row.get("execActor") == "server" and step in (
                    "exec_fail",
                    "exec_req_fail",
                ):
                    storage.set_status(key, ctx.obj["endpoint"], address, "terminal")
                elif row.get("execActor") == "server" and step in (
                    "jwt_pass",
                    "exec_req_pass",
                    "exec_pass",
                ):
                    storage.set_status(key, ctx.obj["endpoint"], address, "accepted")
                if (
                    step == "exec_pass" and row.get("execActor") == "server"
                ) or step == "exec_finality_observed":
                    history_data = keeper.request(
                        ctx.obj["endpoint"], keeper.HISTORY, {"trader": address}
                    )
                    matches = [
                        item
                        for item in history_data["perp"]["tradeHistory"]
                        if item.get("intentKey") == key and item.get("trade")
                    ]
                    data["matchedTradeHistory"] = matches
                    if matches:
                        storage.set_status(
                            key, ctx.obj["endpoint"], address, "terminal"
                        )
        elif key and data.get("userIntents") == []:
            # Keeper has no record. Past its admission expiry it never will,
            # so the key no longer blocks new trades.
            local = storage.mark_absent(
                key, ctx.obj["endpoint"], address, keeper.ABSENT_AFTER_SECONDS
            )
            if local is not None:
                data["localStatus"] = local
        emit(ctx, data)
    except keeper.KeeperError as exc:
        raise click.ClickException(str(exc)) from exc


@intent.command("local")
@click.argument("address")
@click.pass_context
def intent_local(ctx: click.Context, address: str) -> None:
    """List locally recorded intent identities for ADDRESS."""
    emit(ctx, storage.list_pending(ctx.obj["endpoint"], address))


@main.group()
def trade() -> None:
    """Submit authenticated trading intents."""


def submit(ctx: click.Context, payload: dict[str, Any], dry_run: bool) -> None:
    try:
        if dry_run:
            emit(ctx, {"dryRun": True, "intent": payload})
            return
        emit(ctx, intents.submit(ctx.obj["endpoint"], session(ctx), payload))
    except (ValueError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


@trade.command("open")
@click.option("--market", "market_id", type=int, required=True)
@click.option("--side", type=click.Choice(["long", "short"]), required=True)
@click.option(
    "--type",
    "order_type",
    type=click.Choice(["market", "limit", "stop"]),
    required=True,
)
@click.option("--amount", required=True, help="USDC amount in USD.")
@click.option("--leverage", required=True)
@click.option(
    "--price",
    help="Required trigger for limit/stop; optional dry-run reference for market orders. Live market orders use a refreshed price.",
)
@click.option("--slippage", required=True, help="Maximum slippage percentage.")
@click.option("--take-profit")
@click.option("--stop-loss")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def trade_open(
    ctx: click.Context,
    market_id: int,
    side: str,
    order_type: str,
    amount: str,
    leverage: str,
    price: str | None,
    slippage: str,
    take_profit: str | None,
    stop_loss: str | None,
    dry_run: bool,
) -> None:
    """Open a market, limit, or stop trade."""
    try:
        payload = intents.open_intent(
            market_id,
            side,
            order_type,
            amount,
            leverage,
            price,
            slippage,
            take_profit,
            stop_loss,
        )
        if order_type == "market" and dry_run:
            emit(
                ctx,
                {"dryRun": True, "requiresMarketPriceRefresh": True, "intent": payload},
            )
        elif order_type == "market":
            active = session(ctx)
            if price is not None:
                click.echo(
                    "Market orders use the refreshed Keeper price; --price is a dry-run reference.",
                    err=True,
                )
            markets = keeper.request(ctx.obj["endpoint"], keeper.MARKETS)["perp"][
                "borrowings"
            ]
            candidates = [
                item
                for item in markets
                if item["marketId"] == market_id
                and item["collateralToken"]["symbol"] == "USDC"
                and item["isOpen"]
            ]
            if len(candidates) != 1:
                raise ValueError("Current open USDC market data is unavailable")
            current_price = format(Decimal(str(candidates[0]["price"])), "f")
            intents.positive(current_price, "Current price")
            payload["body"]["intentPrice"] = current_price
            result = intents.submit(ctx.obj["endpoint"], active, payload)
            result["referencePrice"] = current_price
            emit(ctx, result)
        else:
            submit(ctx, payload, dry_run)
    except (ValueError, KeyError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


@trade.command("close")
@click.argument("trade_id", type=int)
@click.option(
    "--cancel-order",
    is_flag=True,
    help="Cancel a pending order instead of closing a position.",
)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def trade_close(
    ctx: click.Context, trade_id: int, cancel_order: bool, dry_run: bool
) -> None:
    """Close a position or cancel a pending order."""
    try:
        submit(ctx, intents.close_intent(trade_id, cancel_order), dry_run)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@trade.command("tpsl")
@click.argument("trade_id", type=int)
@click.option("--take-profit")
@click.option("--stop-loss")
@click.option("--clear-take-profit", is_flag=True)
@click.option("--clear-stop-loss", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def trade_tpsl(
    ctx: click.Context,
    trade_id: int,
    take_profit: str | None,
    stop_loss: str | None,
    clear_take_profit: bool,
    clear_stop_loss: bool,
    dry_run: bool,
) -> None:
    """Set or clear take-profit and stop-loss prices."""
    try:
        submit(
            ctx,
            intents.tpsl_intent(
                trade_id, take_profit, stop_loss, clear_take_profit, clear_stop_loss
            ),
            dry_run,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@trade.command("leverage")
@click.argument("trade_id", type=int)
@click.option("--leverage", required=True, help="Target leverage as a decimal.")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def trade_leverage(
    ctx: click.Context, trade_id: int, leverage: str, dry_run: bool
) -> None:
    """Set target leverage without changing TP or SL."""
    try:
        submit(ctx, intents.leverage_intent(trade_id, leverage), dry_run)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@trade.command("margin")
@click.argument("trade_id", type=int)
@click.option("--direction", type=click.Choice(["add", "remove"]), required=True)
@click.option(
    "--amount",
    required=True,
    help="USDC amount for a leverage quote, not an execution cap.",
)
@click.option(
    "--lcd-endpoint",
    default=margin.MAINNET_LCD,
    show_default=True,
    help="Nibiru REST endpoint for exact contract state.",
)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def trade_margin(
    ctx: click.Context,
    trade_id: int,
    direction: str,
    amount: str,
    lcd_endpoint: str,
    dry_run: bool,
) -> None:
    """Add or remove margin through a target-leverage intent.

    The amount is a quote, not a cap: collateral can change before execution.
    """
    try:
        active = session(ctx)
        raw, funds, market = margin.snapshot(
            ctx.obj["endpoint"], lcd_endpoint, active["evmAddress"], trade_id
        )
        quoted = margin.margin_quote(raw, funds, market, direction, amount)
        payload = intents.leverage_intent(trade_id, quoted["requestedLeverage"])
        warning = "The request authorizes target leverage, not an amount cap; the actual margin change may differ if the position changes before execution."
        if dry_run:
            emit(
                ctx,
                {
                    "dryRun": True,
                    "quote": quoted,
                    "intent": payload,
                    "warning": warning,
                },
            )
            return
        fresh_raw, fresh_funds, fresh_market = margin.snapshot(
            ctx.obj["endpoint"], lcd_endpoint, active["evmAddress"], trade_id
        )
        fresh_quote = margin.margin_quote(
            fresh_raw, fresh_funds, fresh_market, direction, amount
        )
        if (
            fresh_raw["collateral_amount"] != raw["collateral_amount"]
            or fresh_raw["leverage"] != raw["leverage"]
            or fresh_quote != quoted
        ):
            raise ValueError("Position changed; review the margin change again")
        result = intents.submit(ctx.obj["endpoint"], active, payload)
        result["quote"] = quoted
        result["warning"] = warning
        emit(ctx, result)
    except (ValueError, KeyError, TypeError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


@trade.command("partial-close")
@click.argument("trade_id", type=int)
@click.option("--percent", type=click.IntRange(1, 99), required=True)
@click.option(
    "--lcd-endpoint",
    default=margin.MAINNET_LCD,
    show_default=True,
    help="Nibiru REST endpoint for exact contract state.",
)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def partial_close(
    ctx: click.Context, trade_id: int, percent: int, lcd_endpoint: str, dry_run: bool
) -> None:
    """Reduce an open USDC position by a whole percentage."""
    try:
        active = session(ctx)
        # The snapshot refuses when Keeper's index differs from the contract,
        # so the reduction is never sized from a lagging collateral amount.
        raw, _, borrowing = margin.snapshot(
            ctx.obj["endpoint"], lcd_endpoint, active["evmAddress"], trade_id
        )
        market_id = borrowing["marketId"]
        markets = keeper.request(ctx.obj["endpoint"], keeper.MARKETS)["perp"][
            "borrowings"
        ]
        candidates = [
            item
            for item in markets
            if item["marketId"] == market_id
            and item["collateralToken"]["symbol"] == "USDC"
        ]
        if len(candidates) != 1 or not candidates[0]["isOpen"]:
            raise ValueError("Open market data not available")
        position = {
            "id": trade_id,
            "collateralAmount": raw["collateral_amount"],
            "leverage": raw["leverage"],
        }
        payload = intents.partial_intent(
            position, percent, Decimal(str(candidates[0]["minPositionSizeUSD"]))
        )
        if dry_run:
            emit(ctx, {"dryRun": True, "intent": payload})
        else:
            latest, _, _ = margin.snapshot(
                ctx.obj["endpoint"], lcd_endpoint, active["evmAddress"], trade_id
            )
            if (
                latest["collateral_amount"] != raw["collateral_amount"]
                or latest["leverage"] != raw["leverage"]
            ):
                raise ValueError("Position changed; review the reduction again")
            emit(ctx, intents.submit(ctx.obj["endpoint"], active, payload))
    except (ValueError, KeyError, TypeError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def watch() -> None:
    """Stream live Keeper updates as JSON lines."""


@watch.command("positions")
@click.argument("address")
@click.option("--ws-endpoint")
@click.pass_context
def watch_positions(ctx: click.Context, address: str, ws_endpoint: str | None) -> None:
    """Follow position snapshots for ADDRESS until interrupted."""
    watch_stream(
        ws_endpoint or derive_ws(ctx.obj["endpoint"]),
        "subscription($user:String!) { perpTrades(where:{trader:$user}) { id trader isOpen isLong tradeType leverage openPrice tp sl } }",
        {"user": address},
    )


@watch.command("balances")
@click.argument("address")
@click.option("--ws-endpoint")
@click.pass_context
def watch_balances(ctx: click.Context, address: str, ws_endpoint: str | None) -> None:
    """Follow balances, preserving Sai deposit and credit labels."""
    watch_stream(
        ws_endpoint or derive_ws(ctx.obj["endpoint"]),
        "subscription($user:String!) { userBalances(where:{user:$user}) { amount usd_value token_info { name symbol decimals type } } }",
        {"user": address},
    )


@watch.command("price")
@click.argument("token_id", type=int)
@click.option("--ws-endpoint")
@click.pass_context
def watch_price(ctx: click.Context, token_id: int, ws_endpoint: str | None) -> None:
    """Follow the current price of TOKEN_ID."""
    watch_stream(
        ws_endpoint or derive_ws(ctx.obj["endpoint"]),
        "subscription($id:Int!) { tokenPricesUsd(where:{tokenId:$id}) { priceUsd token { id symbol } lastUpdatedBlock { block block_ts } } }",
        {"id": token_id},
    )


def derive_ws(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise click.ClickException("Cannot derive WebSocket endpoint")
    return parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws").geturl()


def watch_stream(
    ws_endpoint: str,
    query: str,
    variables: dict[str, Any],
    once: bool = False,
    ctx: click.Context | None = None,
) -> None:
    parsed = urlsplit(ws_endpoint)
    if parsed.scheme != "wss" and not (
        parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise click.ClickException(
            "WebSocket endpoint must use WSS, except on loopback"
        )
    while True:
        try:
            with connect(
                ws_endpoint,
                subprotocols=[Subprotocol("graphql-transport-ws")],
                open_timeout=10,
            ) as socket:
                socket.send(json.dumps({"type": "connection_init"}))
                message = json.loads(socket.recv(timeout=10))
                if message.get("type") != "connection_ack":
                    raise ValueError("Keeper did not acknowledge WebSocket connection")
                socket.send(
                    json.dumps(
                        {
                            "id": "1",
                            "type": "subscribe",
                            "payload": {"query": query, "variables": variables},
                        }
                    )
                )
                click.echo("Connected; waiting for current snapshot", err=True)
                while True:
                    message = json.loads(socket.recv(timeout=20 if once else None))
                    if message.get("type") == "ping":
                        socket.send(json.dumps({"type": "pong"}))
                    elif message.get("type") == "next":
                        payload = message.get("payload")
                        expected = next(
                            (
                                name
                                for name in (
                                    "userBalances",
                                    "perpTrades",
                                    "tokenPricesUsd",
                                )
                                if name in query
                            ),
                            None,
                        )
                        if (
                            not isinstance(payload, dict)
                            or payload.get("errors")
                            or not isinstance(payload.get("data"), dict)
                            or expected is None
                            or not isinstance(payload["data"].get(expected), list)
                        ):
                            raise click.ClickException(
                                "Keeper subscription returned incomplete data"
                            )
                        if once:
                            if ctx is not None:
                                emit(ctx, payload)
                            return
                        click.echo(json.dumps(payload, separators=(",", ":")))
                    elif message.get("type") in ("error", "complete"):
                        raise click.ClickException("Keeper subscription ended")
        except (OSError, TimeoutError, WebSocketException) as exc:
            if once:
                raise click.ClickException("Could not read balance snapshot") from exc
            click.echo(
                f"Disconnected ({type(exc).__name__}); data may be stale; reconnecting in 2s",
                err=True,
            )
            time.sleep(2)


if __name__ == "__main__":
    main()
