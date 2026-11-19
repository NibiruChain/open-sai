"""Sai trader CLI with Keeper queries, intents, and live feeds."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import click

from . import mcp_install
from .core import (
    clipboard,
    keeper,
    reconcile,
    storage,
    stream,
    trading,
    update,
)
from .core import session as sessions
from .core import settings as sai_settings
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


# Global flags that override a setting: option name, setting key.
GLOBAL_FLAGS = (
    ("--endpoint", "keeper.endpoint"),
    ("--ws-endpoint", "keeper.ws_endpoint"),
    ("--lcd-endpoint", "lcd.endpoint"),
)


def settings(ctx: click.Context) -> sai_settings.Settings:
    """Resolve settings once per command: flags, environment, config file,
    then mainnet defaults."""
    obj = ctx.find_root().obj
    if obj.get("settings") is None:
        try:
            obj["settings"] = sai_settings.load(obj["flags"], warn=warn)
        except sai_settings.SettingsError as exc:
            raise click.ClickException(str(exc)) from exc
    return obj["settings"]


def keeper_url(ctx: click.Context) -> str:
    return settings(ctx).endpoint


def session(ctx: click.Context) -> dict[str, str]:
    return sessions.load_active_session(keeper_url(ctx), ctx.obj["as_addr"])


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
    help="Keeper GraphQL HTTP endpoint. Overrides SAI_ENDPOINT and"
    " keeper.endpoint in the config file.",
)
@click.option(
    "--ws-endpoint",
    help="Keeper WebSocket endpoint. Overrides SAI_WS_ENDPOINT and"
    " keeper.ws_endpoint; derived from the Keeper endpoint when unset.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru REST endpoint. Overrides SAI_LCD_ENDPOINT and lcd.endpoint.",
)
@click.option(
    "--json", "json_output", is_flag=True, help="Compact machine-readable JSON."
)
@click.option(
    "--as",
    "as_addr",
    metavar="ADDR",
    help="Run this command as another connected account instead of the"
    " active one. See `sai auth list`.",
)
@click.pass_context
def main(
    ctx: click.Context,
    endpoint: str | None,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
    json_output: bool,
    as_addr: str | None,
) -> None:
    """Inspect Sai markets and account data, and submit trading intents.

    Settings come from flags, then SAI_* environment variables, then
    $SAI_HOME/config.toml (see `sai config`), then mainnet defaults.
    """
    ctx.ensure_object(dict)
    passed = (endpoint, ws_endpoint, lcd_endpoint)
    flags = {key: value for (_, key), value in zip(GLOBAL_FLAGS, passed, strict=True)}
    ctx.obj.update(flags=flags, settings=None, json=json_output, as_addr=as_addr)
    if ctx.invoked_subcommand is None:
        repl(ctx)


def forwarded_flags(obj: dict[str, Any]) -> list[str]:
    """The global flags the person passed when starting the REPL, so each
    command still reads the config file for the rest."""
    args: list[str] = []
    for option, key in GLOBAL_FLAGS:
        value = obj["flags"].get(key)
        if value is not None:
            args += [option, value]
    if obj["json"]:
        args.append("--json")
    if obj.get("as_addr"):
        args += ["--as", obj["as_addr"]]
    return args


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
                    args=[*forwarded_flags(ctx.obj), *args],
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
    "--addr",
    metavar="ADDR",
    help="EVM addr to sign in as. With --account it skips `cast wallet"
    " address`; with --wallet it must match the ud wallet's addr.",
)
@click.pass_context
def login(
    ctx: click.Context, account: str | None, addr: str | None, wallet: str | None
) -> None:
    """Sign Keeper's trading-session challenge with a local key.

    Use --account for a Foundry keystore: sai runs `cast wallet address` and
    `cast wallet sign`, and cast may prompt for the keystore password. If you
    use the `ud` tool, --wallet signs with a ud wallet instead. The CLI checks
    that the challenge is a Sai `UserTradingSession` for this addr before
    anything signs it.
    """
    if bool(account) == bool(wallet):
        raise click.UsageError("Pass exactly one of --account or --wallet")
    try:
        result = signed_login(keeper_url(ctx), account, wallet, addr)
        result["endpoint"] = keeper_url(ctx)
        storage.save_session(result)
        emit(ctx, sessions.summary(result))
    except (ValueError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


def checked_address(found: str, expected: str | None) -> str:
    """Return FOUND, or fail when the --addr cross-check disagrees."""
    if expected is not None and expected.lower() != found.lower():
        raise ValueError(f"--addr {expected} does not match the wallet addr {found}")
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
    endpoint: str, account: str | None, wallet: str | None, addr: str | None
) -> dict[str, str]:
    """Sign in with a Foundry keystore (--account) or a ud wallet (--wallet).

    --account never looks for ud: it signs with cast even when ud is
    installed and knows a wallet by the same name.
    """
    if addr is not None and not keeper.is_address(addr):
        raise ValueError(f"Not an EVM addr: {addr}")
    if account:
        if addr is None:
            addr = keeper.cast_address(account)
        return keeper.cast_session(endpoint, addr, account)
    assert wallet
    if shutil.which("ud") is None:
        raise ValueError(
            "--wallet needs the `ud` tool on PATH; use --account NAME with a"
            " Foundry keystore, or `sai auth import`"
        )
    found = keeper.ud_wallet(wallet)
    if found is None:
        raise ValueError(
            f"`ud evm wallet show {wallet}` failed or printed no addr;"
            " check that ud knows this wallet"
        )
    if not found.can_sign:
        raise ValueError(ud_state_hint(wallet, found.cast_state))
    addr = checked_address(found.addr, addr)
    return keeper.wallet_session(endpoint, addr, wallet)


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
    the token. Prints the addr, expiry, and endpoint, never the token.
    """
    try:
        token = read_token()
        result = sessions.import_session(token, keeper_url(ctx))
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
@click.argument("addr", required=False)
@click.pass_context
def logout(ctx: click.Context, addr: str | None) -> None:
    """Remove ADDR, or the active account, from this machine.

    Other connected accounts stay connected; when the active one is removed,
    none is active until `sai auth use`. Makes no network call. Keeper cannot
    revoke session tokens yet, so a token copied elsewhere stays valid until
    it expires.
    """
    try:
        removed = storage.delete_session(addr or ctx.obj["as_addr"])
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    emit(
        ctx,
        {
            "removed": removed is not None,
            "addr": None if removed == storage.UNREADABLE_REMOVED else removed,
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
        emit(ctx, {"accounts": account_rows(keeper_url(ctx))})
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@auth.command("use")
@click.argument("addr")
@click.pass_context
def auth_use(ctx: click.Context, addr: str) -> None:
    """Make the connected ADDR the active account.

    Commands without `--as` act on the active account. Sign in again to
    connect a new one: `sai auth login` and `sai auth import` add accounts.
    """
    try:
        emit(ctx, {**sessions.summary(storage.use_session(addr)), "active": True})
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
        emit(ctx, keeper.request(keeper_url(ctx), query, variables))
    except keeper.KeeperError as exc:
        raise click.ClickException(str(exc)) from exc


@account.command("positions")
@click.argument("addr")
@click.pass_context
def positions(ctx: click.Context, addr: str) -> None:
    """List positions and pending orders for ADDR."""
    perform_query(ctx, keeper.POSITIONS, {"trader": addr})


@account.command("history")
@click.argument("addr")
@click.pass_context
def history(ctx: click.Context, addr: str) -> None:
    """Read trade history for ADDR."""
    perform_query(ctx, keeper.HISTORY, {"trader": addr})


@account.command("balances")
@click.argument("addr")
@click.option("--ws-endpoint", help="Override the keeper.ws_endpoint setting.")
@click.pass_context
def balances(ctx: click.Context, addr: str, ws_endpoint: str | None) -> None:
    """Read one account balance snapshot from Keeper's subscription."""
    try:
        emit(
            ctx,
            stream.snapshot(
                ws_url(ctx, ws_endpoint),
                stream.BALANCES,
                {"user": addr},
                notify=warn,
            ),
        )
    except stream.StreamError as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def intent() -> None:
    """Inspect and reconcile trading request outcomes."""


@intent.command("status")
@click.argument("addr")
@click.option("--key", help="Specific intent key.")
@click.pass_context
def intent_status(ctx: click.Context, addr: str, key: str | None) -> None:
    """Read Keeper's latest intent records; reconcile a known key."""
    try:
        emit(ctx, reconcile.reconcile(keeper_url(ctx), addr, key))
    except keeper.KeeperError as exc:
        raise click.ClickException(str(exc)) from exc


@intent.command("local")
@click.argument("addr")
@click.pass_context
def intent_local(ctx: click.Context, addr: str) -> None:
    """List locally recorded intent identities for ADDR."""
    emit(ctx, storage.list_pending(keeper_url(ctx), addr))


@main.group()
def trade() -> None:
    """Submit authenticated trading intents."""


class PinnedSession:
    """Load the --as account, or the active one, and keep using that addr.

    The first load fixes the addr, so the output names the account that
    submitted even if another process switches accounts meanwhile.
    """

    def __init__(self, ctx: click.Context) -> None:
        self.ctx = ctx
        self.addr: str | None = None

    def __call__(self) -> dict[str, str]:
        if self.addr is None:
            result = session(self.ctx)
        else:
            result = sessions.load_active_session(keeper_url(self.ctx), self.addr)
        self.addr = result["evmAddress"]
        return result


def loader(ctx: click.Context) -> PinnedSession:
    """Return this command's session loader; it loads only when called."""
    return ctx.obj.setdefault("loader", PinnedSession(ctx))


def emit_trade(ctx: click.Context, result: dict[str, Any]) -> None:
    """Print a trade result with the account it used or would use.

    A dry run without a usable session names no account, unless --as asked
    for one, which then must be connected.
    """
    load = loader(ctx)
    account = load.addr
    if account is None:
        try:
            account = load()["evmAddress"]
        except ValueError:
            # A dry run needs no session, but a requested account must exist.
            if ctx.obj["as_addr"]:
                raise
            account = None
    emit(ctx, {"addr": account, **result})


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
                keeper_url(ctx),
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
                keeper_url(ctx),
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
                keeper_url(ctx),
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
                keeper_url(ctx),
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
    help="Nibiru REST endpoint for exact contract state. Defaults to the"
    " lcd.endpoint setting.",
)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def trade_margin(
    ctx: click.Context,
    trade_id: int,
    direction: str,
    amount: str,
    lcd_endpoint: str | None,
    dry_run: bool,
) -> None:
    """Add or remove margin through a target-leverage intent.

    The amount is a quote, not a cap: collateral can change before execution.
    """
    try:
        emit_trade(
            ctx,
            trading.change_margin(
                keeper_url(ctx),
                loader(ctx),
                trade_id=trade_id,
                direction=direction,
                amount=amount,
                lcd_endpoint=lcd_url(ctx, lcd_endpoint),
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
    help="Nibiru REST endpoint for exact contract state. Defaults to the"
    " lcd.endpoint setting.",
)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def partial_close(
    ctx: click.Context,
    trade_id: int,
    percent: int,
    lcd_endpoint: str | None,
    dry_run: bool,
) -> None:
    """Reduce an open USDC position by a whole percentage."""
    try:
        emit_trade(
            ctx,
            trading.partial_close(
                keeper_url(ctx),
                loader(ctx),
                trade_id=trade_id,
                percent=percent,
                lcd_endpoint=lcd_url(ctx, lcd_endpoint),
                dry_run=dry_run,
            ),
        )
    except (ValueError, KeyError, TypeError, keeper.KeeperError) as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("doctor")
@click.option(
    "--offline", is_flag=True, help="Skip the Keeper, WebSocket, and LCD checks."
)
@click.option("--no-fix", is_flag=True, help="Report problems without fixing any.")
@click.option(
    "--ws-endpoint",
    help="Keeper WebSocket endpoint. Defaults to the keeper.ws_endpoint setting.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru REST endpoint. Defaults to the lcd.endpoint setting.",
)
@click.pass_context
def doctor_command(
    ctx: click.Context,
    offline: bool,
    no_fix: bool,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
) -> None:
    """Check the install, session, intent ledger, and endpoints, and fix what
    is safe to fix.

    Fixes: create $SAI_HOME/trader, make it and its files private, migrate
    the intent ledger, and reconcile unknown intents for the signed-in
    account with Keeper (a read; no trade is placed or cancelled). It never
    deletes or rewrites the session, and never prints its token. Each check
    prints pass, fixed, warn, fail, or skipped, with a fix for the rest. Exits
    1 if any check still fails.
    """
    from . import doctor

    config = dataclasses.replace(
        settings(ctx),
        ws_endpoint=ws_url(ctx, ws_endpoint),
        lcd_endpoint=lcd_url(ctx, lcd_endpoint),
    )
    checks = doctor.run_checks(doctor.context(config, fix=not no_fix, offline=offline))
    if ctx.obj["json"]:
        click.echo(doctor.render_json(doctor.report(checks)))
    else:
        click.echo(doctor.render_text(checks))
    if doctor.overall(checks) == "fail":
        ctx.exit(1)


@main.group()
def watch() -> None:
    """Stream live Keeper updates as JSON lines."""


@watch.command("positions")
@click.argument("addr")
@click.option("--ws-endpoint")
@click.pass_context
def watch_positions(ctx: click.Context, addr: str, ws_endpoint: str | None) -> None:
    """Follow position snapshots for ADDR until interrupted."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.POSITIONS,
        {"user": addr},
    )


@watch.command("balances")
@click.argument("addr")
@click.option("--ws-endpoint")
@click.pass_context
def watch_balances(ctx: click.Context, addr: str, ws_endpoint: str | None) -> None:
    """Follow balances, preserving Sai deposit and credit labels."""
    watch_stream(
        ws_url(ctx, ws_endpoint),
        stream.BALANCES,
        {"user": addr},
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
    """The command's --ws-endpoint, else the resolved WebSocket setting."""
    return ws_endpoint or settings(ctx).ws_endpoint


def lcd_url(ctx: click.Context, lcd_endpoint: str | None) -> str:
    """The command's --lcd-endpoint, else the resolved LCD setting."""
    return lcd_endpoint or settings(ctx).lcd_endpoint


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


@main.group("config")
def config_group() -> None:
    """Show and change settings in $SAI_HOME/config.toml.

    Keys: keeper.endpoint, keeper.ws_endpoint, lcd.endpoint, update.auto.
    A flag or SAI_* environment variable still overrides the file.
    """


def config_key(key: str) -> sai_settings.Setting:
    """Return KEY's registry entry; an unknown key is a usage error (exit 2)."""
    try:
        return sai_settings.setting(key)
    except sai_settings.SettingsError as exc:
        raise click.UsageError(str(exc)) from exc


def config_file() -> Path:
    return sai_settings.config_path(os.environ)


@config_group.command("list")
@click.pass_context
def config_list(ctx: click.Context) -> None:
    """Show every setting with its value and source.

    The source is flag, env, file, default, or derived. URLs are shown
    without credentials; `sai config get` prints the exact value.
    """
    shown = sai_settings.public(settings(ctx))
    if ctx.obj["json"]:
        emit(ctx, {"path": str(config_file()), "settings": shown})
        return
    click.echo(f"# {config_file()}")
    for key, entry in shown.items():
        value = json.dumps(entry["value"])
        click.echo(f"{key} = {value}  # {entry['source']}, {entry['env']}")


@config_group.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Print the exact value of KEY as resolved now."""
    spec = config_key(key)
    resolved = settings(ctx)
    value = resolved.value(spec.key)
    if ctx.obj["json"]:
        emit(
            ctx,
            {"key": spec.key, "value": value, "source": resolved.sources[spec.key]},
        )
        return
    click.echo(json.dumps(value) if isinstance(value, bool) else value)


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set KEY to VALUE in the config file, keeping its comments."""
    spec = config_key(key)
    path = config_file()
    try:
        parsed = sai_settings.parse_value(spec.key, value)
    except sai_settings.SettingsError as exc:
        raise click.UsageError(
            f"{exc}. Valid keys: {', '.join(sai_settings.KEYS)}"
        ) from exc
    try:
        sai_settings.edit_file(
            path, lambda text: sai_settings.set_value(text, spec.key, value, path)[0]
        )
    except sai_settings.UneditableSection as exc:
        raise click.UsageError(str(exc)) from exc
    except sai_settings.SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    report_change(ctx, spec, {"key": spec.key, "value": parsed, "path": str(path)})


@config_group.command("unset")
@click.argument("key")
@click.pass_context
def config_unset(ctx: click.Context, key: str) -> None:
    """Remove KEY from the config file so the default applies again."""
    spec = config_key(key)
    path = config_file()
    removed: list[bool] = []

    def edit(text: str | None) -> str | None:
        new, was_set = sai_settings.unset_value(text, spec.key, path)
        removed.append(was_set)
        # An already unset key leaves the file untouched.
        return new if was_set else None

    try:
        sai_settings.edit_file(path, edit)
    except sai_settings.UneditableSection as exc:
        raise click.UsageError(str(exc)) from exc
    except sai_settings.SettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    report_change(
        ctx, spec, {"key": spec.key, "removed": removed[0], "path": str(path)}
    )


def report_change(
    ctx: click.Context, spec: sai_settings.Setting, result: dict[str, Any]
) -> None:
    """Print RESULT, and warn when a flag or variable still overrides it."""
    if ctx.obj["json"]:
        emit(ctx, result)
    elif "value" in result:
        click.echo(f"Set {spec.key} in {result['path']}")
    else:
        verb = "Unset" if result["removed"] else "Already unset:"
        click.echo(f"{verb} {spec.key} in {result['path']}")
    if os.environ.get(spec.env, "").strip():
        warn(f"Warning: {spec.env} is set and overrides {spec.key} in the file")


@main.group("mcp", invoke_without_command=True)
@click.pass_context
def mcp_group(ctx: click.Context) -> None:
    """Run Sai as an MCP server for agent clients.

    Register it with Claude Code, Codex, or Cursor with
    `sai mcp install claude` (or codex, cursor, --all).
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


SERVE_TTY_NOTICE = (
    "sai mcp serve speaks MCP over stdin and stdout; an agent client should"
    " launch it; register it with `sai mcp install`."
    " Press Ctrl-C to stop."
)


@mcp_group.command("serve")
@click.option(
    "--endpoint",
    help="Keeper GraphQL HTTP endpoint. Defaults to `sai --endpoint` when"
    " given, then SAI_ENDPOINT, then keeper.endpoint in the config file,"
    " then mainnet.",
)
@click.option(
    "--ws-endpoint",
    help="Keeper WebSocket endpoint. Defaults to `sai --ws-endpoint`, then"
    " SAI_WS_ENDPOINT, then keeper.ws_endpoint, then one derived from the"
    " Keeper endpoint.",
)
@click.option(
    "--lcd-endpoint",
    help="Nibiru LCD endpoint for margin and partial-close reads. Defaults to"
    " `sai --lcd-endpoint`, then SAI_LCD_ENDPOINT, then lcd.endpoint, then"
    " mainnet.",
)
@click.pass_context
def mcp_serve(
    ctx: click.Context,
    endpoint: str | None,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
) -> None:
    """Serve Sai tools to an agent client over stdio.

    Stdout carries only MCP protocol messages. Every tool is listed; trade
    tools submit only while an account is signed in with `sai auth login`
    or `sai auth import`. Endpoints resolve once, at start, like the CLI's:
    these flags, then SAI_* variables, then $SAI_HOME/config.toml.
    """
    from .mcp_server import server

    flags = serve_flags(
        ctx.find_root().obj["flags"], endpoint, ws_endpoint, lcd_endpoint
    )
    if sys.stdin.isatty():
        click.echo(SERVE_TTY_NOTICE, err=True)
    server.serve(flags)


def serve_flags(
    global_flags: dict[str, str | None],
    endpoint: str | None,
    ws_endpoint: str | None,
    lcd_endpoint: str | None,
) -> dict[str, str | None]:
    """The flag layer for `sai mcp serve`: its own options, then the same
    `sai` global flags."""
    passed = (endpoint, ws_endpoint, lcd_endpoint)
    return {
        key: value if value is not None else global_flags.get(key)
        for (_, key), value in zip(GLOBAL_FLAGS, passed, strict=True)
    }


class ArgvAfterDash(click.Command):
    """Keep everything after `--` as the server command instead of CLIENTs."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if "--" in args:
            at = args.index("--")
            ctx.meta["server_argv"] = args[at + 1 :]
            if not ctx.meta["server_argv"]:
                raise click.UsageError("Give the server command after `--`.", ctx)
            args = args[:at]
        return super().parse_args(ctx, args)


def interactive() -> bool:
    """Prompt only when a person is at both ends; agents never see prompts."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def mcp_clients() -> dict[str, mcp_install.Client]:
    return mcp_install.make_clients(Path.home(), os.environ, Path.cwd())


def chosen_clients(names: tuple[str, ...]) -> list[str]:
    try:
        return list(dict.fromkeys(mcp_install.normalize_client(n) for n in names))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def pick(prompt: str, options: list[str]) -> list[str]:
    """Ask on the terminal which of options to act on; blank means all."""
    for number, name in enumerate(options, 1):
        click.echo(f"  {number}. {name}", err=True)
    answer = click.prompt(prompt, default="all", err=True)
    if answer.strip().lower() == "all":
        return options
    picked = []
    for part in answer.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= len(options):
            picked.append(options[int(part) - 1])
        else:
            picked.append(part)
    return chosen_clients(tuple(picked))


def server_target(ctx: click.Context) -> mcp_install.Target:
    target, warnings = mcp_install.server_target(
        ctx.meta.get("server_argv"),
        os.environ,
        Path.home(),
        mcp_install.running_prefix(),
    )
    for message in warnings:
        warn(message)
    return target


def quiet_target(ctx: click.Context) -> mcp_install.Target | None:
    """The command install would register, or None; prints nothing."""
    try:
        target, _ = mcp_install.server_target(
            ctx.meta.get("server_argv"),
            os.environ,
            Path.home(),
            mcp_install.running_prefix(),
        )
    except mcp_install.InstallError:
        return None
    return target


def refuse(ctx: click.Context, exc: mcp_install.InstallError) -> None:
    emit(ctx, {"error": str(exc), "code": exc.code})
    ctx.exit(1)


def finish(ctx: click.Context, results: list[mcp_install.Result]) -> None:
    summary = mcp_install.summarize(results)
    emit(ctx, summary)
    if summary["reconnectNeeded"]:
        warn("Restart or reconnect the changed clients to load the change.")
    if any(r.client == "cursor" and r.action in ("added", "replaced") for r in results):
        warn(mcp_install.Cursor.HINT)
    if any(r.action == "failed" for r in results):
        ctx.exit(1)


SCOPE_OPTION = click.option(
    "--scope",
    type=click.Choice(mcp_install.SCOPES),
    default="user",
    show_default=True,
    help="user: every project. project: this directory's shared config"
    " (Claude Code .mcp.json, Cursor .cursor/mcp.json). Codex is user only.",
)


@mcp_group.command("list")
@click.pass_context
def mcp_list(ctx: click.Context) -> None:
    """Show detected agent clients and where Sai is registered.

    `current` is true when the entry runs the command `install` would
    register. Only the sai entry is read; other servers are never shown.
    """
    try:
        target = server_target(ctx)
        command: list[str] | None = target.argv
    except mcp_install.InstallError as exc:
        warn(str(exc))
        target, command = None, None
    emit(
        ctx,
        {
            "command": command,
            "clients": mcp_install.list_clients(mcp_clients(), target),
        },
    )


@mcp_group.command("install", cls=ArgvAfterDash)
@click.argument("clients", nargs=-1)
@click.option("--all", "all_clients", is_flag=True, help="Every detected client.")
@click.option("--dry-run", is_flag=True, help="Show the changes; make none.")
@SCOPE_OPTION
@click.pass_context
def mcp_install_command(
    ctx: click.Context,
    clients: tuple[str, ...],
    all_clients: bool,
    dry_run: bool,
    scope: str,
) -> None:
    """Register `sai mcp serve` with Claude Code, Codex, or Cursor.

    CLIENT is claude, codex, or cursor. Registers the stable `sai` on PATH,
    normally ~/.local/bin/sai, so updates need no re-registering. For a dev
    checkout, give the server command after `--`, for example
    `sai mcp install claude -- uv run --directory ~/open-sai sai mcp serve`.

    With no CLIENT and no --all, a terminal asks which clients to register;
    without a terminal, this lists detected clients and changes nothing.
    """
    found = mcp_clients()
    try:
        target = server_target(ctx)
    except mcp_install.InstallError as exc:
        refuse(ctx, exc)
        return
    names = chosen_clients(clients)
    detected = [n for n, c in found.items() if c.detected()[0]]
    if all_clients:
        names = list(dict.fromkeys([*names, *detected]))
    if not names:
        if not interactive():
            emit(
                ctx,
                {
                    "command": target.argv,
                    "clients": mcp_install.list_clients(found, target),
                },
            )
            warn(
                "Nothing changed. Name clients, as in `sai mcp install claude`,"
                " or pass --all."
            )
            return
        if not detected:
            raise click.UsageError("No agent clients found; name one to register.")
        names = pick("Register Sai with (numbers, names, or all)", detected)
    results = [mcp_install.install_one(found[n], scope, target, dry_run) for n in names]
    finish(ctx, results)


@mcp_group.command("uninstall")
@click.argument("clients", nargs=-1)
@click.option("--all", "all_clients", is_flag=True, help="Every client.")
@click.option("--dry-run", is_flag=True, help="Show the changes; make none.")
@click.option(
    "--scope",
    type=click.Choice(mcp_install.UNINSTALL_SCOPES),
    help="Remove only this scope. Default: every scope sai is registered in"
    " (Claude Code local entries are this directory's).",
)
@click.pass_context
def mcp_uninstall(
    ctx: click.Context,
    clients: tuple[str, ...],
    all_clients: bool,
    dry_run: bool,
    scope: str | None,
) -> None:
    """Remove Sai from Claude Code, Codex, or Cursor.

    Removes sai from every scope it is registered in for each client, unless
    --scope names one. With no CLIENT and no --all, a terminal asks which to
    remove; without a terminal this is a usage error that names where Sai is
    registered.
    """
    found = mcp_clients()
    names = chosen_clients(clients)
    target = quiet_target(ctx)
    if all_clients:
        names = list(found)
    if not names:
        rows = mcp_install.list_clients(found, target)
        registered = [r["client"] for r in rows if r["registered"]]
        where = ", ".join(
            f"{r['client']} ({', '.join(e['scope'] for e in r['registrations'])})"
            for r in rows
            if r["registered"]
        )
        if not interactive():
            raise click.UsageError(
                "Name clients to remove, or pass --all. Sai is registered with: "
                + (where or "no client")
                + "."
            )
        if not registered:
            raise click.UsageError("Sai is not registered with any client.")
        names = pick("Remove Sai from (numbers, names, or all)", registered)
    results = [
        r
        for n in names
        for r in mcp_install.uninstall_client(found[n], scope, dry_run, target)
    ]
    finish(ctx, results)


# Exit code for an install `sai update` cannot change in place.
EXIT_UNSUPPORTED = 3


def update_report(
    installed: str, latest: str, install: update.Install
) -> dict[str, Any]:
    newer = update.is_newer(latest, installed)
    return {
        "installed": installed,
        "latest": latest.removeprefix("v"),
        "latestTag": latest,
        "updateAvailable": newer,
        "install": install.to_json(),
        "update": asdict(update.guidance(install, latest)) if newer else None,
    }


def print_update_report(report: dict[str, Any]) -> None:
    kind = report["install"]["kind"]
    click.echo(f"installed  {report['installed']} ({kind})")
    click.echo(f"latest     {report['latest']}")
    if not report["updateAvailable"]:
        click.echo("sai is up to date.")
        return
    guide = report["update"]
    click.echo(f"A newer version is available. {guide['note']}")
    if guide["command"]:
        click.echo(f"  {guide['command']}")


@main.command("update")
@click.option("--check", is_flag=True, help="Report the latest version only.")
@click.option("--api-base", default=update.API_BASE, hidden=True)
@click.option("--web-base", default=update.WEB_BASE, hidden=True)
@click.pass_context
def update_command(
    ctx: click.Context, check: bool, api_base: str, web_base: str
) -> None:
    """Compare the installed version with the latest GitHub release.

    `--check` reports the installed and latest versions, how sai was
    installed, and how to update it. Installing updates comes in a later
    release.
    """
    if not check:
        click.echo(
            "`sai update` cannot install updates yet; that comes in a later"
            " release. Run `sai update --check` to see the latest version and"
            " how to update this install.",
            err=True,
        )
        ctx.exit(EXIT_UNSUPPORTED)
    try:
        latest = update.fetch_latest(api_base=api_base, web_base=web_base)
    except update.UpdateError as exc:
        raise click.ClickException(f"update check failed: {exc}") from exc
    try:
        update.write_cache(update.Cache(latest, update.now()))
    except OSError as exc:
        warn(f"Warning: could not save the update check: {exc}")
    report = update_report(package_version(), latest, update.install_kind())
    if ctx.obj["json"]:
        emit(ctx, report)
    else:
        print_update_report(report)


if __name__ == "__main__":
    main()
