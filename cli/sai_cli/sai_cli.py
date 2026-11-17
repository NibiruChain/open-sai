"""Sai trader CLI with Keeper queries, intents, and live feeds."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import time
from decimal import Decimal
from importlib import metadata, resources
from typing import Any
from urllib.parse import urlsplit

import click
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect
from websockets.typing import Subprotocol

from .core import clipboard, intents, keeper, margin, reconcile, storage
from .core import session as sessions
from .utils.repl_skin import ReplSkin

PACKAGE = "sai-cli"


def package_version() -> str:
    """Return the installed package version; pyproject.toml sets it."""
    return metadata.version(PACKAGE)


def check_skill(data: bytes) -> bytes:
    """Reject a packaged skill that is not a skill file.

    The package copy is a symlink to skills/sai/SKILL.md. A checkout without
    symlink support, such as Windows without core.symlinks, stores the link
    target path as file text instead.
    """
    if not data.startswith(b"---"):
        raise click.ClickException(
            "Packaged SKILL.md has no frontmatter; this build may contain a"
            " symlink path instead of the skill. Reinstall from a checkout"
            " with symlinks enabled."
        )
    return data


def read_packaged_skill() -> bytes:
    return resources.files("sai_cli").joinpath("skills/SKILL.md").read_bytes()


def skill_bytes() -> bytes:
    """Return the agent skill packaged with this version of the CLI."""
    return check_skill(read_packaged_skill())


def print_skill(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(skill_bytes(), nl=False)
    ctx.exit()


def emit(ctx: click.Context, value: Any) -> None:
    click.echo(json.dumps(value, indent=None if ctx.obj["json"] else 2, default=str))


def session(ctx: click.Context) -> dict[str, str]:
    return sessions.load_active_session(ctx.obj["endpoint"])


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
@click.pass_context
def main(ctx: click.Context, endpoint: str, json_output: bool) -> None:
    """Inspect Sai markets and account data, and submit trading intents."""
    ctx.ensure_object(dict)
    ctx.obj.update(endpoint=endpoint, json=json_output)
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
    """Sign in, check, and remove the local trading session."""


@auth.command("login")
@click.option(
    "--account",
    help="Foundry keystore name (~/.foundry/keystores). cast may prompt for"
    " the keystore password.",
)
@click.option(
    "--wallet",
    help="Wallet name for people who use the `ud` tool. Requires `ud`; signs"
    " through `ud evm wallet run` without a prompt.",
)
@click.option(
    "--address",
    help="EVM address to sign in as. With --account it skips `cast wallet"
    " address`; with --wallet it must match the ud wallet's address.",
)
@click.pass_context
def login(
    ctx: click.Context, account: str | None, address: str | None, wallet: str | None
) -> None:
    """Sign Keeper's trading-session challenge with a local key.

    Use --account for a Foundry keystore: sai runs `cast wallet address` and
    `cast wallet sign`, and cast may prompt for the keystore password. If you
    use the `ud` tool, --wallet signs with a ud wallet instead. The CLI checks
    that the challenge is a Sai `UserTradingSession` for this address before
    anything signs it.
    """
    if bool(account) == bool(wallet):
        raise click.UsageError("Pass exactly one of --account or --wallet")
    try:
        result = signed_login(ctx.obj["endpoint"], account, wallet, address)
        result["endpoint"] = ctx.obj["endpoint"]
        storage.save_session(result)
        emit(ctx, sessions.summary(result))
    except (ValueError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


def checked_address(found: str, expected: str | None) -> str:
    """Return FOUND, or fail when the --address cross-check disagrees."""
    if expected is not None and expected.lower() != found.lower():
        raise ValueError(
            f"--address {expected} does not match the wallet address {found}"
        )
    return found


def ud_state_hint(wallet: str, state: str | None) -> str:
    """Explain why ud cannot sign with WALLET and how to fix it."""
    prefix = f"ud wallet {wallet} has Cast keystore state {state or 'unknown'}"
    if state == "missing":
        return f"{prefix}; run `ud evm wallet sync {wallet}` to create it"
    if state == "name-conflict":
        return (
            f"{prefix}: another keystore already uses that name; rename it,"
            f" then run `ud evm wallet sync {wallet}`"
        )
    return f"{prefix}; check `ud evm wallet show {wallet}`"


def signed_login(
    endpoint: str, account: str | None, wallet: str | None, address: str | None
) -> dict[str, str]:
    """Sign in with a Foundry keystore (--account) or a ud wallet (--wallet).

    --account never looks for ud: it signs with cast even when ud is
    installed and knows a wallet by the same name.
    """
    if address is not None and not keeper.is_address(address):
        raise ValueError(f"Not an EVM address: {address}")
    if account:
        if address is None:
            address = keeper.cast_address(account)
        return keeper.cast_session(endpoint, address, account)
    assert wallet
    if shutil.which("ud") is None:
        raise ValueError(
            "--wallet needs the `ud` tool on PATH; use --account NAME with a"
            " Foundry keystore, or `sai auth import`"
        )
    found = keeper.ud_wallet(wallet)
    if found is None:
        raise ValueError(
            f"`ud evm wallet show {wallet}` failed or printed no address;"
            " check that ud knows this wallet"
        )
    if not found.can_sign:
        raise ValueError(ud_state_hint(wallet, found.cast_state))
    address = checked_address(found.address, address)
    return keeper.wallet_session(endpoint, address, wallet)


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
        result = sessions.import_session(token, ctx.obj["endpoint"])
        storage.save_session(result)
        # Re-read: the person may have copied something else since, and that
        # must survive.
        copied = clipboard.read()
        if copied is not None and copied[0] == token and not clipboard.clear(copied[1]):
            click.echo("Warning: could not clear the clipboard", err=True)
        emit(ctx, sessions.summary(result))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@auth.command("logout")
@click.pass_context
def logout(ctx: click.Context) -> None:
    """Delete the stored session from this machine.

    Makes no network call. Keeper cannot revoke session tokens yet, so a
    token copied elsewhere stays valid until it expires.
    """
    removed = storage.delete_session()
    emit(
        ctx,
        {
            "removed": removed,
            "note": "Keeper cannot revoke session tokens yet; the token stays"
            " valid until it expires.",
        },
    )


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show the account and expiration without showing the token."""
    try:
        result = session(ctx)
        emit(ctx, sessions.summary(result))
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
        emit(ctx, reconcile.reconcile(ctx.obj["endpoint"], address, key))
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
