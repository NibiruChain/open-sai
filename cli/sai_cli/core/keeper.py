"""Keeper GraphQL transport and request construction."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

MAINNET_HTTP = "https://sai-keeper.nibiru.fi/query"
MAINNET_WS = "wss://sai-keeper.nibiru.fi/query"

MARKETS = """query { perp { borrowings { marketId price isOpen visible minLeverage maxLeverage minPositionSizeUSD openFeePct closeFeePct baseToken { id symbol name } quoteToken { symbol } collateralToken { id symbol } } } }"""
MARKET = """query($market:Int!, $collateral:Int!) { perp { borrowing(marketId:$market,collateralId:$collateral) { marketId isOpen minLeverage maxLeverage collateralToken { id symbol } } } }"""
PRICES = """query { oracle { tokenPricesUsd(limit: 100) { priceUsd token { id symbol name } lastUpdatedBlock { block block_ts } } } }"""
POSITIONS = """query($trader:String!) { perp { trades(where:{trader:$trader,isOpen:true}, limit:100) { id trader isOpen isLong tradeType collateralAmount leverage openPrice tp sl state { liquidationPrice pnlCollateral pnlPct positionValue } perpBorrowing { marketId baseToken { symbol } collateralToken { symbol } } } } }"""
POSITION = """query($trader:String!, $id:Int!) { perp { trade(trader:$trader,id:$id) { id trader isOpen tradeType collateralAmount leverage perpBorrowing { marketId collateralToken { id symbol } } } } }"""
ROUTE = """query { oneClickTrading { enabled chainId contractAddress } }"""
HISTORY = """query($trader:String!) { perp { tradeHistory(where:{trader:$trader}, limit:50, order_desc:true) { id intentKey tradeChangeType txHash evmTxHash block { block block_ts } trade { id trader } } } }"""
INTENTS = """query($user:String!, $key:String) { userIntents(where:{user:$user, intentKey:$key}, limit:50) { intentKey kind lifecycleStep execActor evmAddress txHash execBlock execBlockTs errorCode errorMsg } }"""
CHALLENGE = """mutation($address:String!) { createUserIntentAuthChallenge(evmAddress:$address) { nonce evmAddress expiresAt chainId typedData } }"""
SESSION = """mutation($signature:String!, $challenge:String!) { createUserIntentSession(signature:$signature, challenge:$challenge) { token evmAddress expiresAt } }"""
SUBMIT = """mutation($input:RecordUserIntentInput!) { recordUserIntent(input:$input) { intentKey kind lifecycleStep execActor evmAddress txHash errorCode errorMsg } }"""


class KeeperError(Exception):
    """Keeper rejected a query or transport failed."""


class AdmissionRejected(KeeperError):
    """Keeper confirmed that admission was rejected."""


class OutcomeUnknown(KeeperError):
    """The server may have accepted the request."""


class NotSent(KeeperError):
    """The connection failed before the request reached Keeper."""


# Keeper fails a recorded intent that waits this long without execution
# (`expireQueued` in sai-keeper). A key that Keeper has not recorded by then,
# plus a margin for clock skew and slow requests, was never admitted.
ADMISSION_EXPIRY_SECONDS = 120
ABSENT_AFTER_SECONDS = ADMISSION_EXPIRY_SECONDS + 180


def request(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    url = urlsplit(endpoint)
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise KeeperError("Keeper endpoint must use HTTPS, except on loopback")
    if url.username or url.password:
        raise KeeperError("Credentials are not allowed in the Keeper URL")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.post(
            endpoint,
            json={"query": query, "variables": variables or {}},
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise NotSent("Could not connect to Keeper; the request was not sent") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise KeeperError("Keeper request failed or returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise KeeperError("Keeper returned an invalid response")
    if result.get("errors"):
        errors = result["errors"]
        if (
            query == SUBMIT
            and not result.get("data")
            and isinstance(errors, list)
            and errors
            and all(
                isinstance(item, dict)
                and isinstance(item.get("extensions"), dict)
                and item["extensions"].get("code") == "INTENT_NOT_ACCEPTED"
                for item in errors
            )
        ):
            raise AdmissionRejected(
                "Keeper rejected admission; check inputs and session"
            )
        raise KeeperError("Keeper rejected the request; check inputs and session")
    data = result.get("data")
    if not isinstance(data, dict):
        raise KeeperError("Keeper returned no data")
    return data


def wallet_session(endpoint: str, address: str, wallet: str) -> dict[str, str]:
    """Sign public typed challenge with ud. Signature and JWT stay in memory."""
    challenge = request(endpoint, CHALLENGE, {"address": address})[
        "createUserIntentAuthChallenge"
    ]
    if challenge["evmAddress"].lower() != address.lower():
        raise KeeperError("Challenge address does not match the requested account")
    typed_data = challenge["typedData"]
    declared = {field["name"] for field in typed_data["types"]["EIP712Domain"]}
    domain = typed_data["domain"]
    typed_data["domain"] = {
        key: value
        for key, value in domain.items()
        if key in declared or value not in (None, "")
    }
    if any(typed_data["domain"].get(key) in (None, "") for key in declared):
        raise KeeperError("Challenge has an empty declared signing domain field")
    with tempfile.TemporaryDirectory(prefix="sai-auth-") as temp:
        path = Path(temp) / "challenge.json"
        path.write_text(json.dumps(typed_data), encoding="utf-8")
        path.chmod(0o600)
        try:
            proc = subprocess.run(
                [
                    "ud",
                    "evm",
                    "wallet",
                    "run",
                    wallet,
                    "--",
                    "wallet",
                    "sign",
                    "--data",
                    "--from-file",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeeperError("Wallet signing failed") from exc
    signature = proc.stdout.strip()
    if not signature.startswith("0x") or len(signature) != 132:
        raise KeeperError("Wallet signer returned an invalid signature")
    session = request(
        endpoint, SESSION, {"signature": signature, "challenge": challenge["nonce"]}
    )["createUserIntentSession"]
    if session["evmAddress"].lower() != address.lower():
        raise KeeperError("Session address does not match the requested account")
    return session
