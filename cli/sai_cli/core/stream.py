"""Keeper GraphQL subscriptions over graphql-transport-ws."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect
from websockets.typing import Subprotocol

BALANCES = "subscription($user:String!) { userBalances(where:{user:$user}) { amount usd_value token_info { name symbol decimals type } } }"
POSITIONS = "subscription($user:String!) { perpTrades(where:{trader:$user}) { id trader isOpen isLong tradeType leverage openPrice tp sl } }"
PRICE = "subscription($id:Int!) { tokenPricesUsd(where:{tokenId:$id}) { priceUsd token { id symbol } lastUpdatedBlock { block block_ts } } }"

# The list field each subscription must return.
FIELDS = ("userBalances", "perpTrades", "tokenPricesUsd")

CONNECTED = "Connected; waiting for current snapshot"

# Transport failures a caller may retry by reconnecting.
DISCONNECTS = (OSError, TimeoutError, WebSocketException)


class StreamError(ValueError):
    """Keeper's subscription refused, ended, or sent incomplete data."""


def _ignore(_message: str) -> None:
    return None


def derive_ws(endpoint: str) -> str:
    """Return the WebSocket URL that serves the Keeper HTTP ENDPOINT."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise StreamError("Cannot derive WebSocket endpoint")
    return parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws").geturl()


def check_ws(ws_endpoint: str) -> None:
    """Refuse a plain-text WebSocket URL except on loopback."""
    parsed = urlsplit(ws_endpoint)
    if parsed.scheme != "wss" and not (
        parsed.scheme == "ws" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise StreamError("WebSocket endpoint must use WSS, except on loopback")


def check_payload(payload: Any, query: str) -> dict[str, Any]:
    """Return PAYLOAD if it holds the list field that QUERY subscribes to."""
    expected = next((name for name in FIELDS if name in query), None)
    if (
        not isinstance(payload, dict)
        or payload.get("errors")
        or not isinstance(payload.get("data"), dict)
        or expected is None
        or not isinstance(payload["data"].get(expected), list)
    ):
        raise StreamError("Keeper subscription returned incomplete data")
    return payload


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parse one protocol message, refusing anything but a JSON object."""
    try:
        message = json.loads(raw)
    except ValueError as exc:
        raise StreamError("Keeper sent an invalid WebSocket message") from exc
    if not isinstance(message, dict):
        raise StreamError("Keeper sent an invalid WebSocket message")
    return message


def payloads(
    ws_endpoint: str,
    query: str,
    variables: dict[str, Any],
    timeout: float | None = None,
    notify: Callable[[str], None] = _ignore,
) -> Generator[dict[str, Any]]:
    """Connect once and yield each checked snapshot payload.

    Raises StreamError when Keeper does not acknowledge the connection, sends
    an invalid message, or fails or ends the subscription, and one of DISCONNECTS
    when the connection drops or a read exceeds TIMEOUT seconds.
    """
    with connect(
        ws_endpoint,
        subprotocols=[Subprotocol("graphql-transport-ws")],
        open_timeout=10,
    ) as socket:
        socket.send(json.dumps({"type": "connection_init"}))
        message = decode(socket.recv(timeout=10))
        if message.get("type") != "connection_ack":
            raise StreamError("Keeper did not acknowledge WebSocket connection")
        socket.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "subscribe",
                    "payload": {"query": query, "variables": variables},
                }
            )
        )
        notify(CONNECTED)
        while True:
            message = decode(socket.recv(timeout=timeout))
            if message.get("type") == "ping":
                socket.send(json.dumps({"type": "pong"}))
            elif message.get("type") == "next":
                yield check_payload(message.get("payload"), query)
            elif message.get("type") in ("error", "complete"):
                raise StreamError("Keeper subscription ended")


def snapshot(
    ws_endpoint: str,
    query: str,
    variables: dict[str, Any],
    notify: Callable[[str], None] = _ignore,
) -> dict[str, Any]:
    """Read the first snapshot of a subscription and disconnect."""
    check_ws(ws_endpoint)
    stream = payloads(ws_endpoint, query, variables, timeout=20, notify=notify)
    try:
        return next(stream)
    except DISCONNECTS as exc:
        raise StreamError("Could not read balance snapshot") from exc
    finally:
        stream.close()
