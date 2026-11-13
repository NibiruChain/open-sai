"""Exercise the installed CLI against an isolated GraphQL HTTP service."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from websockets.sync.server import serve
from websockets.typing import Subprotocol


def _resolve_cli() -> list[str]:
    path = shutil.which("sai")
    if path:
        return [path]
    if os.environ.get("CLI_ANYTHING_FORCE_INSTALLED") == "1":
        raise RuntimeError("Installed sai command is required")
    import sys

    return [sys.executable, "-m", "cli_anything.sai"]


@pytest.fixture
def keeper_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "borrowings" in body["query"]
            result = json.dumps(
                {"data": {"perp": {"borrowings": [{"marketId": 0, "price": 100.0}]}}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/query"
    server.shutdown()
    server.server_close()
    thread.join()


def test_installed_sai_command_help():
    result = subprocess.run(
        [*_resolve_cli(), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "Usage: sai " in result.stdout
    assert "market" in result.stdout
    assert "trade" in result.stdout


def test_installed_cli_queries_local_keeper(keeper_server):
    result = subprocess.run(
        [*_resolve_cli(), "--endpoint", keeper_server, "--json", "market", "list"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout)["perp"]["borrowings"] == [
        {"marketId": 0, "price": 100.0}
    ]


def test_installed_cli_receives_balance_snapshot():
    def handler(connection):
        assert json.loads(connection.recv())["type"] == "connection_init"
        connection.send(json.dumps({"type": "connection_ack"}))
        assert json.loads(connection.recv())["type"] == "subscribe"
        connection.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "next",
                    "payload": {
                        "data": {
                            "userBalances": [
                                {
                                    "amount": "10",
                                    "token_info": {
                                        "name": "USDC on Sai",
                                        "symbol": "USDC",
                                        "decimals": 6,
                                        "type": "sai",
                                    },
                                }
                            ]
                        }
                    },
                }
            )
        )

    server = serve(
        handler, "127.0.0.1", 0, subprotocols=[Subprotocol("graphql-transport-ws")]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        result = subprocess.run(
            [
                *_resolve_cli(),
                "--json",
                "account",
                "balances",
                "0xabc",
                "--ws-endpoint",
                f"ws://127.0.0.1:{port}/query",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert (
            json.loads(result.stdout)["data"]["userBalances"][0]["token_info"]["name"]
            == "USDC on Sai"
        )
    finally:
        server.shutdown()
        thread.join()
