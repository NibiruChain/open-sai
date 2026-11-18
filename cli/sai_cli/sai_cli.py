"""Sai trader CLI with Keeper queries, intents, and live feeds."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import time
from datetime import UTC, datetime
from importlib import metadata, resources
from typing import Any

import click

from .core import clipboard, keeper, margin, reconcile, storage, stream, trading
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
    return sessions.load_active_session(ctx.obj["endpoint"], ctx.obj["as_address"])


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
    "--as",
    "as_address",
    metavar="ADDRESS",
    help="Run this command as another connected account instead of the"
    " active one. See `sai auth list`.",
)
@click.pass_context
def main(
    ctx: click.Context, endpoint: str, json_output: bool, as_address: str | None
) -> None:
    """Inspect Sai markets and account data, and submit trading intents."""
    ctx.ensure_object(dict)
    ctx.obj.update(endpoint=endpoint, json=json_output, as_address=as_address)
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
                        *(["--as", a] if (a := ctx.obj.get("as_address")) else []),
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
    """Connect, list, switch, and remove Sai accounts on this machine."""


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
@click.argument("address", required=False)
@click.pass_context
def logout(ctx: click.Context, address: str | None) -> None:
    """Remove ADDRESS, or the active account, from this machine.

    Other connected accounts stay connected; when the active one is removed,
    none is active until `sai auth use`. Makes no network call. Keeper cannot
    revoke session tokens yet, so a token copied elsewhere stays valid until
    it expires.
    """
    try:
        removed = storage.delete_session(address or ctx.obj["as_address"])
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(
        ctx,
        {
            "removed": removed is not None,
            "address": None if removed == storage.UNREADABLE_REMOVED else removed,
            "note": "Keeper cannot revoke session tokens yet; the token stays"
            " valid until it expires.",
        },
    )


def account_rows(endpoint: str) -> list[dict[str, Any]]:
    """Describe each connected account without its token."""
    accounts = storage.load_accounts()
    now = datetime.now(UTC)
    return [
        {
            **sessions.summary(result),
            "active": key == accounts["active"],
            "expired": datetime.fromisoformat(result["expiresAt"]) <= now,
            "endpointMatches": result.get("endpoint") == endpoint,
        }
        for key, result in sorted(accounts["sessions"].items())
    ]


@auth.command("list")
@click.pass_context
def auth_list(ctx: click.Context) -> None:
    """List connected accounts, their expiry, and which one is active."""
    try:
        emit(ctx, {"accounts": account_rows(ctx.obj["endpoint"])})
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@auth.command("use")
@click.argument("address")
@click.pass_context
def auth_use(ctx: click.Context, address: str) -> None:
    """Make the connected ADDRESS the active account.

    Commands without `--as` act on the active account. Sign in again to
    connect a new one: `sai auth login` and `sai auth import` add accounts.
    """
    try:
        emit(ctx, {**sessions.summary(storage.use_session(address)), "active": True})
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show the active account, or the --as account, and its expiry without
    showing the token."""
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
    try:
        emit(
            ctx,
            stream.snapshot(
                ws_url(ctx, ws_endpoint),
                stream.BALANCES,
                {"user": address},
                notify=warn,
            ),
        )
    except stream.StreamError as exc:
        raise click.ClickException(str(exc)) from exc


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


class PinnedSession:
    """Load the --as account, or the active one, and keep using that address.

    The first load fixes the address, so the output names the account that
    submitted even if another process switches accounts meanwhile.
    """

    def __init__(self, ctx: click.Context) -> None:
        self.ctx = ctx
        self.address: str | None = None

    def __call__(self) -> dict[str, str]:
        if self.address is None:
            result = session(self.ctx)
        else:
            result = sessions.load_active_session(
                self.ctx.obj["endpoint"], self.address
            )
        self.address = result["evmAddress"]
        return result


def loader(ctx: click.Context) -> PinnedSession:
    """Return this command's session loader; it loads only when called."""
    return ctx.obj.setdefault("loader", PinnedSession(ctx))


def emit_trade(ctx: click.Context, result: dict[str, Any]) -> None:
    """Print a trade result with the account it used or would use.

    A dry run without a usable session names no account.
    """
    load = loader(ctx)
    account = load.address
    if account is None:
        try:
            account = load()["evmAddress"]
        except ValueError:
            account = None
    emit(ctx, {"account": account, **result})


def warn(message: str) -> None:
    click.echo(message, err=True)


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
        emit_trade(
            ctx,
            trading.open_trade(
                ctx.obj["endpoint"],
                loader(ctx),
                market_id=market_id,
                side=side,
                order_type=order_type,
                amount=amount,
                leverage=leverage,
                price=price,
                slippage=slippage,
                take_profit=take_profit,
                stop_loss=stop_loss,
                dry_run=dry_run,
                notify=warn,
            ),
        )
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
        emit_trade(
            ctx,
            trading.close_trade(
                ctx.obj["endpoint"],
                loader(ctx),
                trade_id=trade_id,
                cancel_order=cancel_order,
                dry_run=dry_run,
            ),
        )
    except (ValueError, keeper.KeeperError) as exc:
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
        emit_trade(
            ctx,
            trading.set_tpsl(
                ctx.obj["endpoint"],
                loader(ctx),
                trade_id=trade_id,
                take_profit=take_profit,
                stop_loss=stop_loss,
                clear_take_profit=clear_take_profit,
                clear_stop_loss=clear_stop_loss,
                dry_run=dry_run,
            ),
        )
    except (ValueError, keeper.KeeperError) as exc:
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
        emit_trade(
            ctx,
            trading.set_leverage(
                ctx.obj["endpoint"],
                loader(ctx),
                trade_id=trade_id,
                leverage=leverage,
                dry_run=dry_run,
            ),
        )
    except (ValueError, keeper.KeeperError) as exc:
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
        emit_trade(
            ctx,
            trading.change_margin(
                ctx.obj["endpoint"],
                loader(ctx),
                trade_id=trade_id,
                direction=direction,
                amount=amount,
                lcd_endpoint=lcd_endpoint,
                dry_run=dry_run,
            ),
        )
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
        emit_trade(
            ctx,
            trading.partial_close(
                ctx.obj["endpoint"],
                loader(ctx),
                trade_id=trade_id,
                percent=percent,
                lcd_endpoint=lcd_endpoint,
                dry_run=dry_run,
            ),
        )
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
        ws_url(ctx, ws_endpoint),
        stream.POSITIONS,
        {"user": address},
    )


@watch.command("balances")
@click.argument("address")
@click.option("--ws-endpoint")
@click.pass_context
def watch_balances(ctx: click.Context, address: str, ws_endpoint: str | None) -> None:
    """Follow balances, preserving Sai deposit and credit labels."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.BALANCES,
        {"user": address},
    )


@watch.command("price")
@click.argument("token_id", type=int)
@click.option("--ws-endpoint")
@click.pass_context
def watch_price(ctx: click.Context, token_id: int, ws_endpoint: str | None) -> None:
    """Follow the current price of TOKEN_ID."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.PRICE,
        {"id": token_id},
    )


def ws_url(ctx: click.Context, ws_endpoint: str | None) -> str:
    try:
        return ws_endpoint or stream.derive_ws(ctx.obj["endpoint"])
    except stream.StreamError as exc:
        raise click.ClickException(str(exc)) from exc


def watch_stream(ws_endpoint: str, query: str, variables: dict[str, Any]) -> None:
    """Print each snapshot as a JSON line, reconnecting until interrupted."""
    try:
        stream.check_ws(ws_endpoint)
        while True:
            try:
                for payload in stream.payloads(
                    ws_endpoint, query, variables, notify=warn
                ):
                    click.echo(json.dumps(payload, separators=(",", ":")))
            except stream.DISCONNECTS as exc:
                click.echo(
                    f"Disconnected ({type(exc).__name__}); data may be stale;"
                    " reconnecting in 2s",
                    err=True,
                )
                time.sleep(2)
    except stream.StreamError as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
