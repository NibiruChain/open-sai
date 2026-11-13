"""Sai trader CLI with Keeper queries, intents, and live feeds."""

from __future__ import annotations

import base64
import json
import os
import shlex
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import click
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect
from websockets.typing import Subprotocol

from .core import intents, keeper, storage
from .utils.repl_skin import ReplSkin


def emit(ctx: click.Context, value: Any) -> None:
    click.echo(json.dumps(value, indent=None if ctx.obj["json"] else 2, default=str))


def session(ctx: click.Context) -> dict[str, str]:
    fd = ctx.obj.get("passphrase_fd")
    password = (
        os.read(fd, 4096).decode().rstrip("\r\n")
        if fd is not None
        else click.prompt("Session passphrase", hide_input=True, err=True)
    )
    result = storage.load_session(password)
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
    "--passphrase-fd",
    type=click.IntRange(0),
    help="Read the session passphrase from an inherited file descriptor.",
)
@click.pass_context
def main(
    ctx: click.Context, endpoint: str, json_output: bool, passphrase_fd: int | None
) -> None:
    """Inspect Sai markets and account data, and submit trading intents."""
    ctx.ensure_object(dict)
    ctx.obj.update(endpoint=endpoint, json=json_output, passphrase_fd=passphrase_fd)
    if ctx.invoked_subcommand is None:
        repl(ctx)


def repl(ctx: click.Context) -> None:
    skin = ReplSkin("sai", version="0.1.0")
    skin.print_banner()
    click.echo("Enter a command, or 'exit'.")
    while True:
        try:
            line = input("sai> ")
        except (EOFError, KeyboardInterrupt):
            click.echo()
            break
        if line.strip() in ("exit", "quit"):
            break
        try:
            args = shlex.split(line)
            if args:
                main.main(
                    args=[
                        "--endpoint",
                        ctx.obj["endpoint"],
                        *(["--json"] if ctx.obj["json"] else []),
                        *args,
                    ],
                    standalone_mode=False,
                )
        except (click.ClickException, ValueError, keeper.KeeperError) as exc:
            click.echo(f"Error: {exc}", err=True)


@main.group()
def auth() -> None:
    """Connect and manage a local encrypted trading session."""


@auth.command("login")
@click.option(
    "--address", required=True, help="Public EVM address associated with the wallet."
)
@click.option(
    "--wallet", required=True, help="Wallet name in the local ud wallet manager."
)
@click.pass_context
def login(ctx: click.Context, address: str, wallet: str) -> None:
    """Sign Keeper's challenge using a protected local ud wallet."""
    try:
        result = keeper.wallet_session(ctx.obj["endpoint"], address, wallet)
        result["endpoint"] = ctx.obj["endpoint"]
        password = click.prompt(
            "New session passphrase",
            hide_input=True,
            confirmation_prompt=True,
            err=True,
        )
        storage.save_session(result, password)
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


@auth.command("import")
@click.pass_context
def import_token(ctx: click.Context) -> None:
    """Import an existing session token through a hidden prompt."""
    try:
        token = click.prompt("Sai session token", hide_input=True, err=True)
        result = import_session(token, ctx.obj["endpoint"])
        password = click.prompt(
            "New session passphrase",
            hide_input=True,
            confirmation_prompt=True,
            err=True,
        )
        storage.save_session(result, password)
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


@trade.command("partial-close")
@click.argument("trade_id", type=int)
@click.option("--percent", type=click.IntRange(1, 99), required=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def partial_close(
    ctx: click.Context, trade_id: int, percent: int, dry_run: bool
) -> None:
    """Reduce an open USDC position by a whole percentage."""
    try:
        active = session(ctx)
        data = keeper.request(
            ctx.obj["endpoint"],
            keeper.POSITION,
            {"trader": active["evmAddress"], "id": trade_id},
        )
        item = data["perp"]["trade"]
        rows = (
            [item] if item and item["isOpen"] and item["tradeType"] == "trade" else []
        )
        if (
            len(rows) != 1
            or rows[0]["perpBorrowing"]["collateralToken"]["symbol"] != "USDC"
        ):
            raise ValueError("Open USDC position not found")
        market_id = rows[0]["perpBorrowing"]["marketId"]
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
        payload = intents.partial_intent(
            rows[0], percent, Decimal(str(candidates[0]["minPositionSizeUSD"]))
        )
        if dry_run:
            emit(ctx, {"dryRun": True, "intent": payload})
        else:
            latest = keeper.request(
                ctx.obj["endpoint"],
                keeper.POSITION,
                {"trader": active["evmAddress"], "id": trade_id},
            )["perp"]["trade"]
            if (
                not latest
                or not latest["isOpen"]
                or latest["tradeType"] != "trade"
                or latest["collateralAmount"] != rows[0]["collateralAmount"]
                or latest["leverage"] != rows[0]["leverage"]
            ):
                raise ValueError("Position changed; review the reduction again")
            emit(ctx, intents.submit(ctx.obj["endpoint"], active, payload))
    except (ValueError, KeyError, keeper.KeeperError) as exc:
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
