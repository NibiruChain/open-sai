"""Tool implementations for the Sai MCP server.

Each function takes a `Config` and returns a JSON-ready value, raising the
same errors as the CLI's core. `run` turns a call into a JSON string and
never raises: known errors become `{"error", "code"}` objects, and anything
else becomes a generic error with the traceback logged to stderr.

The server never takes a session token. It reads the session that `sai auth
login` or `sai auth import` stored, the same way the CLI does, without a
prompt, and never returns the token.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..core import intents, keeper, margin, reconcile, storage, stream, trading
from ..core import session as sessions
from . import info

log = logging.getLogger("sai_cli.mcp_server")

NO_ADDRESS = (
    "No address given and no stored Sai session. Pass address, or sign in with"
    " `sai auth login` or `sai auth import` in a terminal."
)
OTHER_ENDPOINT = (
    "No address given, and the stored session belongs to a different Keeper"
    " endpoint than this server's SAI_ENDPOINT, so its address may not be the"
    " account meant here. Pass address explicitly."
)
INTERNAL = "Unexpected error in the Sai MCP server; see its stderr log"
NEEDS_DIGEST = (
    "preview_digest is required: call the matching preview_* tool, show the"
    " user the preview, get confirmation, then pass its previewDigest"
)
NEEDS_REFERENCE = (
    "reference_price is required for a market order: pass the referencePrice"
    " from the preview_open result the user confirmed"
)
MARKET_ONLY_REFERENCE = "reference_price applies only to market orders"
MISMATCH = (
    "The trade no longer matches the preview the user confirmed, so nothing"
    " was submitted. Show the user this new preview, get their confirmation"
    " again, then retry with its previewDigest."
)
RECENT_OPEN = (
    "An open-trade intent for this account from the last {minutes} minutes"
    " is still {statuses}, so this open was not submitted: after a timeout or"
    " error the first request may have gone through. Call intent_local, then"
    " intent_status with each key in intentKeys. Retry with"
    " allow_concurrent_open=true only once the user confirms they want"
    " another open position."
)
PRICE_MOVED = (
    "The market price moved {moved}% from the confirmed referencePrice, more"
    " than the {slippage}% slippage, so nothing was submitted. Show the user"
    " this new preview, get their confirmation again, then retry with its"
    " previewDigest and referencePrice."
)


@dataclass(frozen=True)
class Config:
    """Endpoints the server talks to. SAI_HOME is read by core.storage."""

    endpoint: str = keeper.MAINNET_HTTP
    ws_override: str | None = None
    lcd_endpoint: str = margin.MAINNET_LCD

    @property
    def ws_endpoint(self) -> str:
        """The WebSocket endpoint: SAI_WS_ENDPOINT or derived from ENDPOINT."""
        return self.ws_override or stream.derive_ws(self.endpoint)


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read endpoints from SAI_ENDPOINT, SAI_WS_ENDPOINT, SAI_LCD_ENDPOINT."""
    values = os.environ if env is None else env
    return Config(
        endpoint=values.get("SAI_ENDPOINT") or keeper.MAINNET_HTTP,
        ws_override=values.get("SAI_WS_ENDPOINT") or None,
        lcd_endpoint=values.get("SAI_LCD_ENDPOINT") or margin.MAINNET_LCD,
    )


class SessionUnavailable(ValueError):
    """The stored session is missing, expired, or unusable by the server."""


class Refused(ValueError):
    """A trade was refused before submission; CODE says why.

    PREVIEW, when set, is the fresh preview with its own previewDigest for
    the agent to show the user before any retry.
    """

    def __init__(
        self,
        message: str,
        code: str,
        preview: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.preview = preview
        self.extra = extra


# Errors


def error(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, "code": code, **extra}


def describe(exc: Exception) -> dict[str, Any]:
    """Map a known error to a stable code; messages carry no secrets."""
    if isinstance(exc, keeper.OutcomeUnknown):
        key = exc.intent_key
        return error(
            str(exc),
            "OUTCOME_UNKNOWN",
            intentKey=key,
            next=(
                f"Call intent_status with key {key} to reconcile. Do not"
                " resubmit until it reports a final outcome."
            ),
        )
    if isinstance(exc, keeper.AdmissionRejected):
        return error(str(exc), "ADMISSION_REJECTED")
    if isinstance(exc, keeper.NotSent):
        return error(str(exc), "NOT_SENT")
    if isinstance(exc, keeper.KeeperError):
        return error(str(exc), "KEEPER_ERROR")
    if isinstance(exc, Refused):
        extra = {} if exc.preview is None else {"preview": exc.preview}
        return error(str(exc), exc.code, **extra, **exc.extra)
    if isinstance(exc, SessionUnavailable):
        return error(str(exc), "SESSION_UNAVAILABLE")
    if isinstance(exc, stream.StreamError):
        return error(str(exc), "STREAM_ERROR")
    if isinstance(exc, ValueError):
        return error(str(exc), "INVALID_REQUEST")
    if isinstance(exc, (KeyError, TypeError)):
        return error(
            f"Keeper or contract data was missing or malformed ({type(exc).__name__})",
            "INCOMPLETE_DATA",
        )
    trace = "".join(traceback.format_exception(exc))
    log.error("Unexpected error in Sai MCP tool\n%s", redact(trace))
    return error(INTERNAL, "INTERNAL_ERROR")


REDACTED = "[redacted session token]"


def redact(text: str) -> str:
    """Remove the stored session token from TEXT, if one can be read."""
    try:
        token = storage.load_session()["token"]
    except (ValueError, KeyError):
        token = None
    return text.replace(token, REDACTED) if token else text


def run(action: Callable[[], Any]) -> str:
    """Call ACTION and return its result or error as a JSON string."""
    try:
        value = action()
    except Exception as exc:  # noqa: BLE001 - tools must not raise
        value = describe(exc)
        value["error"] = redact(value["error"])
    return json.dumps(value, default=str)


# Session


def session_loader(config: Config) -> trading.SessionLoader:
    """Return a loader for the active stored session; it never prompts."""

    def load() -> dict[str, str]:
        try:
            return sessions.load_active_session(config.endpoint)
        except ValueError as exc:
            raise SessionUnavailable(str(exc)) from exc

    return load


def resolve_address(config: Config, address: str | None) -> dict[str, Any]:
    """Return ADDRESS, or the stored session's account when it is omitted,
    with where it came from.

    Reads need only the address, so an expired session still supplies it,
    flagged with `sessionExpired`. A session for another endpoint does not.
    """
    if address:
        return {"address": address, "addressSource": "argument"}
    try:
        if not storage.session_exists():
            raise SessionUnavailable(NO_ADDRESS)
        stored = storage.load_session()
        expiry = datetime.fromisoformat(stored["expiresAt"])
    except SessionUnavailable:
        raise
    except ValueError as exc:
        raise SessionUnavailable(f"{exc}. Or pass address.") from exc
    if stored.get("endpoint") != config.endpoint:
        raise SessionUnavailable(OTHER_ENDPOINT)
    return {
        "address": stored["evmAddress"],
        "addressSource": "session",
        "sessionExpired": expiry <= datetime.now(UTC),
        "endpointMatches": True,
    }


def session_state(config: Config) -> dict[str, Any]:
    """Describe the stored session without its token."""
    try:
        if not storage.session_exists():
            return {"signedIn": False}
        stored = storage.load_session()
        expiry = datetime.fromisoformat(stored["expiresAt"])
    except ValueError as exc:
        return {"signedIn": False, "error": str(exc)}
    expired = expiry <= datetime.now(UTC)
    matches = stored.get("endpoint") == config.endpoint
    return {
        "signedIn": True,
        "usable": matches and not expired,
        "address": stored["evmAddress"],
        "expiresAt": stored["expiresAt"],
        "expired": expired,
        "endpoint": info.public_url(stored.get("endpoint")),
        "endpointMatches": matches,
    }


def auth_status(config: Config) -> dict[str, Any]:
    result = sessions.summary(session_loader(config)())
    return {**result, "endpoint": info.public_url(result["endpoint"])}


# Reads


def market_list(config: Config) -> dict[str, Any]:
    return keeper.request(config.endpoint, keeper.MARKETS)


def market_prices(config: Config) -> dict[str, Any]:
    return keeper.request(config.endpoint, keeper.PRICES)


def account_positions(config: Config, address: str | None) -> dict[str, Any]:
    account = resolve_address(config, address)
    variables = {"trader": account["address"]}
    return {**account, **keeper.request(config.endpoint, keeper.POSITIONS, variables)}


def account_history(config: Config, address: str | None) -> dict[str, Any]:
    account = resolve_address(config, address)
    variables = {"trader": account["address"]}
    return {**account, **keeper.request(config.endpoint, keeper.HISTORY, variables)}


def account_balances(config: Config, address: str | None) -> dict[str, Any]:
    account = resolve_address(config, address)
    variables = {"user": account["address"]}
    return {
        **account,
        **stream.snapshot(config.ws_endpoint, stream.BALANCES, variables),
    }


def intent_local(config: Config, address: str | None) -> dict[str, Any]:
    account = resolve_address(config, address)
    pending = storage.list_pending(config.endpoint, account["address"])
    return {**account, "intents": pending}


def intent_status(
    config: Config, address: str | None, key: str | None
) -> dict[str, Any]:
    account = resolve_address(config, address)
    return {
        **account,
        **reconcile.reconcile(config.endpoint, account["address"], key),
    }


# Preview binding


def canonical_price(value: object) -> str:
    """Write a price so equal amounts give equal text: 100, 100.0 -> 100."""
    return format(Decimal(str(value)).normalize(), "f")


def digest_material(preview: dict[str, Any]) -> dict[str, Any]:
    """Return the parts of PREVIEW the user confirms.

    That is the account the trade would be submitted under, the intent, and
    the margin quote when there is one. A market open leaves out its intent
    price, which is refreshed at submit, and binds the referencePrice the
    user confirmed instead.
    """
    intent = copy.deepcopy(preview["intent"])
    body = intent.get("body", {})
    account = preview.get("account")
    material: dict[str, Any] = {
        "account": account.lower() if isinstance(account, str) else None,
        "intent": intent,
    }
    if intent.get("kind") == "OPEN_TRADE" and body.get("orderType") == "market":
        body.pop("intentPrice", None)
        reference = preview.get("referencePrice")
        material["referencePrice"] = (
            canonical_price(reference) if reference is not None else None
        )
    if "quote" in preview:
        material["quote"] = preview["quote"]
    return material


def preview_digest(preview: dict[str, Any]) -> str:
    """Hash the confirmed parts of PREVIEW as canonical JSON."""
    canonical = json.dumps(
        digest_material(preview), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def with_digest(preview: dict[str, Any]) -> dict[str, Any]:
    return {**preview, "previewDigest": preview_digest(preview)}


def preview_account(config: Config) -> str | None:
    """Return the signed-in address a trade would use, or None without one."""
    try:
        return session_loader(config)()["evmAddress"]
    except SessionUnavailable:
        return None


def bound(
    config: Config,
    digest: str | None,
    reference_price: str | None = None,
    slippage: str | None = None,
) -> trading.SubmitCheck:
    """Return a check that refuses any intent but the confirmed one.

    The digest is recomputed for the signed-in account and, for a market
    open, with the caller's REFERENCE_PRICE, so a changed account or
    reference needs a new confirmation. It then refuses a market open whose
    refreshed price moved from REFERENCE_PRICE by more than SLIPPAGE percent.
    """
    if not digest:
        raise ValueError(NEEDS_DIGEST)
    reference = (
        intents.positive(reference_price, "Reference price")
        if reference_price is not None
        else None
    )

    def check(preview: dict[str, Any]) -> None:
        fresh = with_digest({**preview, "account": preview_account(config)})
        confirmed = (
            fresh
            if reference is None
            else {**fresh, "referencePrice": format(reference, "f")}
        )
        if preview_digest(confirmed) != digest:
            raise Refused(MISMATCH, "PREVIEW_MISMATCH", fresh)
        if reference is None or slippage is None:
            return
        current = Decimal(str(preview["referencePrice"]))
        moved = abs(current - reference) / reference * 100
        if moved > Decimal(slippage):
            message = PRICE_MOVED.format(
                moved=format(moved.quantize(Decimal("0.0001")), "f"),
                slippage=slippage,
            )
            raise Refused(message, "PRICE_MOVED", fresh)

    return check


def submit_check(
    config: Config,
    dry_run: bool,
    digest: str | None,
    reference_price: str | None = None,
    slippage: str | None = None,
) -> trading.SubmitCheck:
    """Return the pre-submit check for a trade; a dry run never calls it."""
    if dry_run:
        return lambda _preview: None
    return bound(config, digest, reference_price, slippage)


# Statuses of an open that may still fill, and how long one can. Keeper
# fails a queued intent after its admission expiry; past that margin, an
# open no longer stands in for the one being submitted.
OPEN_PENDING = ("unknown", "accepted")
RECENT_OPEN_SECONDS = keeper.ABSENT_AFTER_SECONDS


def recent_opens(
    endpoint: str, account: str, now: float | None = None
) -> list[dict[str, str]]:
    """Return this account's OPEN_TRADE intents that may still fill.

    The store keeps no payload, so any recent unknown or accepted open
    counts, whatever its market, side, or amount.
    """
    current = time.time() if now is None else now
    with storage.db() as connection:
        rows = connection.execute(
            "SELECT id, status, created_at FROM intents WHERE endpoint=?"
            " AND account=? AND kind='OPEN_TRADE' AND status IN (?, ?)"
            " ORDER BY rowid DESC",
            (endpoint, account.lower(), *OPEN_PENDING),
        ).fetchall()
    recent: list[dict[str, str]] = []
    for key, status, created in rows:
        stamp = created if created is not None else storage._key_time(key)
        if stamp is None or current - stamp <= RECENT_OPEN_SECONDS:
            recent.append({"intentKey": key, "status": status})
    return recent


def no_recent_open(config: Config) -> trading.SubmitCheck:
    """Return a check that refuses an open while a recent one may fill."""

    def check(_preview: dict[str, Any]) -> None:
        account = session_loader(config)()["evmAddress"]
        pending = recent_opens(config.endpoint, account)
        if pending:
            statuses = " or ".join(sorted({row["status"] for row in pending}))
            raise Refused(
                RECENT_OPEN.format(
                    minutes=RECENT_OPEN_SECONDS // 60, statuses=statuses
                ),
                "RECENT_OPEN_PENDING",
                intentKeys=[row["intentKey"] for row in pending],
            )

    return check


def both(
    first: trading.SubmitCheck, second: trading.SubmitCheck
) -> trading.SubmitCheck:
    def check(preview: dict[str, Any]) -> None:
        first(preview)
        second(preview)

    return check


def finish(config: Config, dry_run: bool, result: dict[str, Any]) -> dict[str, Any]:
    """Add the account and previewDigest to a preview; pass a submission
    through."""
    if not dry_run:
        return result
    return with_digest({**result, "account": preview_account(config)})


# Trading: each action previews with dry_run and submits without it. A
# submission rebuilds the intent and goes through only if its digest matches
# the preview the user confirmed; the check runs on the exact intent object
# that is then submitted.


def open_trade(
    config: Config,
    *,
    dry_run: bool,
    market_id: int,
    side: str,
    order_type: str,
    amount: str,
    leverage: str,
    slippage: str,
    price: str | None,
    take_profit: str | None,
    stop_loss: str | None,
    preview_digest: str | None = None,
    reference_price: str | None = None,
    allow_concurrent_open: bool = False,
) -> dict[str, Any]:
    market = order_type == "market"
    if not dry_run and reference_price is not None and not market:
        raise ValueError(MARKET_ONLY_REFERENCE)
    if not dry_run and market and reference_price is None:
        raise ValueError(NEEDS_REFERENCE)
    check = submit_check(
        config, dry_run, preview_digest, reference_price, slippage if market else None
    )
    if not dry_run and not allow_concurrent_open:
        check = both(no_recent_open(config), check)
    notices: list[str] = []
    result = trading.open_trade(
        config.endpoint,
        session_loader(config),
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
        notify=notices.append,
        before_submit=check,
    )
    if dry_run and market:
        # Read-only: the price a live submit would refresh to now.
        current = trading.open_usdc_market(config.endpoint, market_id)["price"]
        result["referencePrice"] = format(Decimal(str(current)), "f")
    if notices:
        result["notices"] = notices
    return finish(config, dry_run, result)


def close_trade(
    config: Config,
    *,
    dry_run: bool,
    trade_id: int,
    cancel_order: bool,
    preview_digest: str | None = None,
) -> dict[str, Any]:
    check = submit_check(config, dry_run, preview_digest)
    result = trading.close_trade(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        cancel_order=cancel_order,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(config, dry_run, result)


def set_tpsl(
    config: Config,
    *,
    dry_run: bool,
    trade_id: int,
    take_profit: str | None,
    stop_loss: str | None,
    clear_take_profit: bool,
    clear_stop_loss: bool,
    preview_digest: str | None = None,
) -> dict[str, Any]:
    check = submit_check(config, dry_run, preview_digest)
    result = trading.set_tpsl(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(config, dry_run, result)


def set_leverage(
    config: Config,
    *,
    dry_run: bool,
    trade_id: int,
    leverage: str,
    preview_digest: str | None = None,
) -> dict[str, Any]:
    check = submit_check(config, dry_run, preview_digest)
    result = trading.set_leverage(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        leverage=leverage,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(config, dry_run, result)


def change_margin(
    config: Config,
    *,
    dry_run: bool,
    trade_id: int,
    direction: str,
    amount: str,
    preview_digest: str | None = None,
) -> dict[str, Any]:
    check = submit_check(config, dry_run, preview_digest)
    result = trading.change_margin(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        lcd_endpoint=config.lcd_endpoint,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(config, dry_run, result)


def partial_close(
    config: Config,
    *,
    dry_run: bool,
    trade_id: int,
    percent: int,
    preview_digest: str | None = None,
) -> dict[str, Any]:
    check = submit_check(config, dry_run, preview_digest)
    result = trading.partial_close(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        percent=percent,
        lcd_endpoint=config.lcd_endpoint,
        dry_run=dry_run,
        before_submit=check,
    )
    return finish(config, dry_run, result)
