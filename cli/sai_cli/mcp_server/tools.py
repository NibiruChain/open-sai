"""Tool implementations for the Sai MCP server.

Each function takes a `Config` and returns a JSON-ready value, raising the
same errors as the CLI's core. `run` turns a call into a JSON string and
never raises: known errors become `{"error", "code"}` objects, and anything
else becomes a generic error with the traceback logged to stderr.

The server never takes a session token or passphrase. It reads the session
that `sai auth login` or `sai auth import` stored, and only the `test`
keyring backend, because an MCP client cannot pass a passphrase.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..core import keeper, margin, reconcile, storage, stream, trading
from ..core import session as sessions

log = logging.getLogger("sai_cli.mcp_server")

FILE_BACKEND = (
    "The stored Sai session uses keyring backend file, which needs a passphrase"
    " the MCP server cannot receive. In a terminal, run `sai auth login` or"
    " `sai auth import` again with the default test keyring backend."
)
NO_ADDRESS = (
    "No address given and no stored Sai session. Pass address, or sign in with"
    " `sai auth login` or `sai auth import` in a terminal."
)
FILE_BACKEND_ADDRESS = (
    "No address given, and the stored session uses keyring backend file, so"
    " the MCP server cannot read its address. Pass address."
)
INTERNAL = "Unexpected error in the Sai MCP server; see its stderr log"


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
    log.error("Unexpected error in Sai MCP tool", exc_info=exc)
    return error(INTERNAL, "INTERNAL_ERROR")


def run(action: Callable[[], Any]) -> str:
    """Call ACTION and return its result or error as a JSON string."""
    try:
        value = action()
    except Exception as exc:  # noqa: BLE001 - tools must not raise
        value = describe(exc)
    return json.dumps(value, default=str)


# Session


def session_loader(config: Config) -> trading.SessionLoader:
    """Return a loader for the active `test`-backend session.

    It refuses a `file`-backend session instead of asking for a passphrase.
    """

    def load() -> dict[str, str]:
        try:
            backend = storage.stored_backend()
            if backend == "file":
                raise SessionUnavailable(FILE_BACKEND)
            return sessions.load_active_session(config.endpoint)
        except SessionUnavailable:
            raise
        except ValueError as exc:
            raise SessionUnavailable(str(exc)) from exc

    return load


def resolve_address(address: str | None) -> str:
    """Return ADDRESS, or the stored session's account when it is omitted.

    Reads need only the address, so an expired session still supplies it.
    """
    if address:
        return address
    try:
        backend = storage.stored_backend()
        if backend is None:
            raise SessionUnavailable(NO_ADDRESS)
        if backend == "file":
            raise SessionUnavailable(FILE_BACKEND_ADDRESS)
        return storage.load_session()["evmAddress"]
    except SessionUnavailable:
        raise
    except ValueError as exc:
        raise SessionUnavailable(f"{exc}. Or pass address.") from exc


def session_state(config: Config) -> dict[str, Any]:
    """Describe the stored session without its token."""
    try:
        backend = storage.stored_backend()
        if backend is None:
            return {"signedIn": False, "backend": None}
        if backend == "file":
            return {
                "signedIn": True,
                "backend": "file",
                "usable": False,
                "address": None,
                "expiresAt": None,
                "expired": None,
                "note": FILE_BACKEND,
            }
        stored = storage.load_session()
        expiry = datetime.fromisoformat(stored["expiresAt"])
    except ValueError as exc:
        return {"signedIn": False, "error": str(exc)}
    expired = expiry <= datetime.now(UTC)
    matches = stored.get("endpoint") == config.endpoint
    return {
        "signedIn": True,
        "backend": "test",
        "usable": matches and not expired,
        "address": stored["evmAddress"],
        "expiresAt": stored["expiresAt"],
        "expired": expired,
        "endpoint": stored.get("endpoint"),
        "endpointMatches": matches,
    }


def auth_status(config: Config) -> dict[str, Any]:
    result = sessions.summary(session_loader(config)())
    return {**result, "backend": "test"}


# Reads


def market_list(config: Config) -> dict[str, Any]:
    return keeper.request(config.endpoint, keeper.MARKETS)


def market_prices(config: Config) -> dict[str, Any]:
    return keeper.request(config.endpoint, keeper.PRICES)


def account_positions(config: Config, address: str | None) -> dict[str, Any]:
    trader = resolve_address(address)
    return keeper.request(config.endpoint, keeper.POSITIONS, {"trader": trader})


def account_history(config: Config, address: str | None) -> dict[str, Any]:
    trader = resolve_address(address)
    return keeper.request(config.endpoint, keeper.HISTORY, {"trader": trader})


def account_balances(config: Config, address: str | None) -> dict[str, Any]:
    user = resolve_address(address)
    return stream.snapshot(config.ws_endpoint, stream.BALANCES, {"user": user})


def intent_local(config: Config, address: str | None) -> list[dict[str, Any]]:
    return storage.list_pending(config.endpoint, resolve_address(address))


def intent_status(
    config: Config, address: str | None, key: str | None
) -> dict[str, Any]:
    return reconcile.reconcile(config.endpoint, resolve_address(address), key)


# Trading: each action previews with dry_run and submits without it.


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
) -> dict[str, Any]:
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
    )
    if notices:
        result["notices"] = notices
    return result


def close_trade(
    config: Config, *, dry_run: bool, trade_id: int, cancel_order: bool
) -> dict[str, Any]:
    return trading.close_trade(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        cancel_order=cancel_order,
        dry_run=dry_run,
    )


def set_tpsl(
    config: Config,
    *,
    dry_run: bool,
    trade_id: int,
    take_profit: str | None,
    stop_loss: str | None,
    clear_take_profit: bool,
    clear_stop_loss: bool,
) -> dict[str, Any]:
    return trading.set_tpsl(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        take_profit=take_profit,
        stop_loss=stop_loss,
        clear_take_profit=clear_take_profit,
        clear_stop_loss=clear_stop_loss,
        dry_run=dry_run,
    )


def set_leverage(
    config: Config, *, dry_run: bool, trade_id: int, leverage: str
) -> dict[str, Any]:
    return trading.set_leverage(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        leverage=leverage,
        dry_run=dry_run,
    )


def change_margin(
    config: Config, *, dry_run: bool, trade_id: int, direction: str, amount: str
) -> dict[str, Any]:
    return trading.change_margin(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        direction=direction,
        amount=amount,
        lcd_endpoint=config.lcd_endpoint,
        dry_run=dry_run,
    )


def partial_close(
    config: Config, *, dry_run: bool, trade_id: int, percent: int
) -> dict[str, Any]:
    return trading.partial_close(
        config.endpoint,
        session_loader(config),
        trade_id=trade_id,
        percent=percent,
        lcd_endpoint=config.lcd_endpoint,
        dry_run=dry_run,
    )
