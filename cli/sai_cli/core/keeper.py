"""Keeper GraphQL transport and request construction."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
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


def _challenge_hex(value: object) -> str:
    """Normalize a 32-byte challenge; Keeper sends the nonce without 0x."""
    if not isinstance(value, str):
        raise TypeError("Challenge must be a string")
    digits = value.lower().removeprefix("0x")
    if len(digits) != 64 or any(c not in "0123456789abcdef" for c in digits):
        raise ValueError("Challenge must be 32 bytes of hex")
    return digits


def check_session_typed_data(
    typed_data: dict[str, Any], address: str, nonce: str
) -> None:
    """Refuse typed data that is not Keeper's session challenge for ADDRESS.

    The signer must never receive a payload the endpoint substituted, such as
    a token permit or an order.
    """
    try:
        domain = typed_data["domain"]
        message = typed_data["message"]
        valid = (
            typed_data["primaryType"] == "UserTradingSession"
            and domain["name"] == "Sai"
            and domain["version"] == "1"
            and isinstance(message["account"], str)
            and message["account"].lower() == address.lower()
            and _challenge_hex(message["challenge"]) == _challenge_hex(nonce)
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise KeeperError("Challenge is not a Sai trading session request") from exc
    if not valid:
        raise KeeperError("Challenge is not a Sai trading session request")


def fetch_challenge(endpoint: str, address: str) -> tuple[dict[str, Any], str]:
    """Return checked typed data to sign and its challenge nonce."""
    challenge = request(endpoint, CHALLENGE, {"address": address})[
        "createUserIntentAuthChallenge"
    ]
    if challenge["evmAddress"].lower() != address.lower():
        raise KeeperError("Challenge address does not match the requested account")
    typed_data = challenge["typedData"]
    check_session_typed_data(typed_data, address, challenge["nonce"])
    declared = {field["name"] for field in typed_data["types"]["EIP712Domain"]}
    domain = typed_data["domain"]
    typed_data["domain"] = {
        key: value
        for key, value in domain.items()
        if key in declared or value not in (None, "")
    }
    if any(typed_data["domain"].get(key) in (None, "") for key in declared):
        raise KeeperError("Challenge has an empty declared signing domain field")
    return typed_data, challenge["nonce"]


def signed_session(
    endpoint: str,
    address: str,
    sign_command: list[str],
    *,
    interactive: bool = False,
) -> dict[str, str]:
    """Sign Keeper's challenge with SIGN_COMMAND plus the typed-data file path.

    With INTERACTIVE, the signer keeps the user's terminal for stdin and
    stderr, so it can prompt for a keystore password. Only stdout, the
    signature, is captured. The signature and JWT stay in memory.
    """
    typed_data, nonce = fetch_challenge(endpoint, address)
    with tempfile.TemporaryDirectory(prefix="sai-auth-") as temp:
        path = Path(temp) / "challenge.json"
        path.write_text(json.dumps(typed_data), encoding="utf-8")
        path.chmod(0o600)
        try:
            proc = subprocess.run(
                [*sign_command, str(path)],
                stdout=subprocess.PIPE,
                stderr=None if interactive else subprocess.PIPE,
                text=True,
                check=True,
                timeout=300 if interactive else 120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeeperError("Wallet signing failed") from exc
    signature = proc.stdout.strip()
    if not signature.startswith("0x") or len(signature) != 132:
        raise KeeperError("Wallet signer returned an invalid signature")
    session = request(endpoint, SESSION, {"signature": signature, "challenge": nonce})[
        "createUserIntentSession"
    ]
    if session["evmAddress"].lower() != address.lower():
        raise KeeperError("Session address does not match the requested account")
    return session


def wallet_session(endpoint: str, address: str, wallet: str) -> dict[str, str]:
    """Sign the challenge with a ud wallet; ud supplies the keystore password."""
    return signed_session(
        endpoint,
        address,
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
        ],
    )


def is_address(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[2:])


@dataclass(frozen=True)
class UdWallet:
    """Public details from `ud evm wallet show`.

    `cast_state` is ud's Cast keystore state for the name: `matching`,
    `missing`, or `name-conflict`, or None when ud printed none. `ud evm
    wallet run` signs only when it is `matching`.
    """

    address: str
    cast_state: str | None

    @property
    def can_sign(self) -> bool:
        return self.cast_state == "matching"


def parse_wallet(output: str) -> UdWallet | None:
    """Read the first JSON object in OUTPUT that has an `address`.

    `ud evm wallet show` prints a JSON object, but tolerate log lines or other
    text around it.
    """
    decoder = json.JSONDecoder()
    start = output.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(output, start)
        except ValueError:
            value = None
        if isinstance(value, dict) and is_address(value.get("address")):
            cast = value.get("cast")
            state = cast.get("state") if isinstance(cast, dict) else None
            return UdWallet(value["address"], state if isinstance(state, str) else None)
        start = output.find("{", start + 1)
    return None


def ud_wallet(wallet: str) -> UdWallet | None:
    """Read a ud wallet's public details, or None if ud cannot show it.

    `ud evm wallet show` prints public data only. Its stdin and stderr are
    closed so it can never prompt, and only the address and Cast state leave
    this function.
    """
    if shutil.which("ud") is None:
        return None
    try:
        proc = subprocess.run(
            ["ud", "evm", "wallet", "show", wallet],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_wallet(proc.stdout)


def cast_address(account: str) -> str:
    """Read a Foundry keystore address; cast may prompt for its password."""
    try:
        proc = subprocess.run(
            ["cast", "wallet", "address", "--account", account],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeeperError(f"cast could not read keystore account {account}") from exc
    address = proc.stdout.strip()
    if not address.startswith("0x") or len(address) != 42:
        raise KeeperError("cast returned an invalid address")
    return address


def cast_session(endpoint: str, address: str, account: str) -> dict[str, str]:
    """Sign the challenge with a Foundry keystore through `cast wallet sign`."""
    return signed_session(
        endpoint,
        address,
        ["cast", "wallet", "sign", "--account", account, "--data", "--from-file"],
        interactive=True,
    )
