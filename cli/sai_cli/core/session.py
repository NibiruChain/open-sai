"""Trading session checks shared by the CLI and other front ends."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from . import log, storage


def load_active_session(endpoint: str, addr: str | None = None) -> dict[str, str]:
    """Load the session for ADDR, or the active account, for ENDPOINT and
    refuse an expired one.

    Never prompts: sessions are encrypted with the CLI's built-in key.
    """
    return check_active(storage.load_session(addr), endpoint)


def check_active(result: dict[str, str], endpoint: str) -> dict[str, str]:
    """Return RESULT if it belongs to ENDPOINT and has not expired."""
    if result.get("endpoint") != endpoint:
        raise ValueError("Stored session belongs to another Keeper endpoint")
    expiry = datetime.fromisoformat(result["expiresAt"])
    if expiry <= datetime.now(UTC):
        log.event(
            "session.expired",
            "warn",
            addr=result.get("evmAddress"),
            expiresAt=result["expiresAt"],
        )
        raise ValueError("Trading session expired; sign in again")
    return result


def import_session(token: str, endpoint: str) -> dict[str, str]:
    """Validate a Sai Web session TOKEN and build the session to store."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        addr = data["sub"]
        expires = datetime.fromtimestamp(data["exp"], UTC)
        if (
            not isinstance(addr, str)
            or not addr.startswith("0x")
            or len(addr) != 42
            or data.get("purpose") != "user_intent"
            or expires <= datetime.now(UTC)
        ):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid or expired Sai session token") from exc
    return {
        "token": token,
        "evmAddress": addr,
        "expiresAt": expires.isoformat(),
        "endpoint": endpoint,
    }


def summary(result: dict[str, str]) -> dict[str, str]:
    """Return the fields safe to show: addr, expiry, endpoint; no token."""
    return {
        "addr": result["evmAddress"],
        "expiresAt": result["expiresAt"],
        "endpoint": result["endpoint"],
    }
