from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from cli_anything.sai.core import intents, keeper, storage
from cli_anything.sai.sai_cli import main
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


@pytest.fixture
def session():
    return {
        "token": "sensitive-token",
        "evmAddress": "0x" + "a" * 40,
        "endpoint": keeper.MAINNET_HTTP,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


def test_encrypted_session_password_and_permissions(session):
    storage.save_session(session, "long passphrase")
    path = storage.home() / "session.json"
    assert b"sensitive-token" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert storage.load_session("long passphrase") == session
    with pytest.raises(ValueError, match="Cannot unlock"):
        storage.load_session("bad passphrase")


def test_open_and_partial_close_payload():
    payload = intents.open_intent(
        3, "long", "limit", "20", "2", "100", "1", "110", None
    )
    assert payload["body"]["market"] == {"marketIndex": "MarketIndex(3)"}
    trade = {"id": 7, "collateralAmount": 10_000_001, "leverage": 2}
    result = intents.partial_intent(trade, 50, Decimal(1))
    assert result["body"]["collateral"] == {"amountUsdc": "5000000", "amountUsd": "5"}
    assert result["body"]["leverageDelta"] == "0"
    with pytest.raises(ValueError):
        intents.partial_intent(trade, 99, Decimal(2))


def test_tpsl_has_keeper_field_patch():
    assert intents.tpsl_intent(3, "12", None, False, True)["body"] == {
        "takeProfit": {"operation": "set", "value": "12"},
        "stopLoss": {"operation": "clear"},
    }


def test_admission_ack_and_key(session, monkeypatch):
    def fake(endpoint, query, variables, token):
        assert token == session["token"]
        submitted = variables["input"]
        assert re.fullmatch(
            r"[0-9]{8}T[0-9]{9}Z_[0-9A-HJKMNP-TV-Z]{16}", submitted["intentKey"]
        )
        assert submitted["lifecycle"]["lifecycleStep"] == "jwt_pass"
        return {
            "recordUserIntent": {
                "intentKey": submitted["intentKey"],
                "lifecycleStep": "jwt_pass",
                "execActor": "server",
                "evmAddress": session["evmAddress"],
            }
        }

    monkeypatch.setattr(keeper, "request", fake)
    result = intents.submit(keeper.MAINNET_HTTP, session, intents.close_intent(1))
    assert result["status"] == "accepted"
    with pytest.raises(ValueError, match="Reconcile"):
        intents.submit(keeper.MAINNET_HTTP, session, intents.close_intent(1))
    assert (
        intents.submit(keeper.MAINNET_HTTP, session, intents.close_intent(2))["status"]
        == "accepted"
    )


def test_uncertain_submission_blocks_retry(session, monkeypatch):
    def fail(*args, **kwargs):
        raise keeper.KeeperError("network")

    monkeypatch.setattr(keeper, "request", fail)
    with pytest.raises(keeper.OutcomeUnknown):
        intents.submit(keeper.MAINNET_HTTP, session, intents.close_intent(1))
    saved = storage.list_pending(keeper.MAINNET_HTTP, session["evmAddress"])
    assert saved[0]["status"] == "unknown"
    with pytest.raises(ValueError, match="Reconcile"):
        intents.submit(keeper.MAINNET_HTTP, session, intents.close_intent(2))


def test_keeper_only_classifies_explicit_rejection(monkeypatch):
    def response(content):
        return httpx.Response(
            200, json=content, request=httpx.Request("POST", "https://example.com")
        )

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: response(
            {"errors": [{"extensions": {"reason": "validation"}}]}
        ),
    )
    with pytest.raises(keeper.KeeperError) as exc:
        keeper.request("https://example.com", keeper.SUBMIT)
    assert not isinstance(exc.value, keeper.AdmissionRejected)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: response(
            {
                "errors": [
                    {
                        "extensions": {
                            "code": "INTENT_NOT_ACCEPTED",
                            "reason": "validation",
                        }
                    }
                ]
            }
        ),
    )
    with pytest.raises(keeper.AdmissionRejected):
        keeper.request("https://example.com", keeper.SUBMIT)


def test_cli_dry_run_does_not_create_session_or_send():
    result = CliRunner().invoke(main, ["--json", "trade", "close", "4", "--dry-run"])
    assert result.exit_code == 0
    assert '"CLOSE_POSITION"' in result.output
    assert not storage.home().exists()


def test_passphrase_file_descriptor(session):
    storage.save_session(session, "secret")
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"secret\n")
    os.close(write_fd)
    try:
        result = CliRunner().invoke(
            main, ["--passphrase-fd", str(read_fd), "auth", "status"]
        )
    finally:
        os.close(read_fd)
    assert result.exit_code == 0, result.output
    assert "sensitive-token" not in result.output


def test_untrusted_status_does_not_resolve_uncertain(session, monkeypatch):
    key = "example"
    storage.pending(
        key, keeper.MAINNET_HTTP, session["evmAddress"], "CLOSE_POSITION", "1"
    )
    monkeypatch.setattr(
        keeper,
        "request",
        lambda *a, **k: {
            "userIntents": [
                {
                    "intentKey": key,
                    "evmAddress": session["evmAddress"],
                    "execActor": "client",
                    "lifecycleStep": "exec_pass",
                }
            ]
        },
    )
    result = CliRunner().invoke(
        main, ["intent", "status", session["evmAddress"], "--key", key]
    )
    assert result.exit_code == 0
    assert (
        storage.list_pending(keeper.MAINNET_HTTP, session["evmAddress"])[0]["status"]
        == "unknown"
    )


def test_rejection_code_without_reason_is_definite(monkeypatch):
    error = {"errors": [{"extensions": {"code": "INTENT_NOT_ACCEPTED"}}]}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            200, json=error, request=httpx.Request("POST", "https://example.com")
        ),
    )
    with pytest.raises(keeper.AdmissionRejected):
        keeper.request("https://example.com", keeper.SUBMIT)


def test_wallet_signing_omits_only_empty_undeclared_domain_fields(monkeypatch, session):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    typed = {
        "types": {"EIP712Domain": [{"name": "name"}, {"name": "version"}]},
        "domain": {
            "name": "Sai",
            "version": "1",
            "chainId": None,
            "salt": "",
            "verifyingContract": "",
        },
        "primaryType": "UserTradingSession",
        "message": {"account": session["evmAddress"]},
    }

    def query(endpoint, query, variables=None, token=None):
        if query == keeper.CHALLENGE:
            return {
                "createUserIntentAuthChallenge": {
                    "evmAddress": session["evmAddress"],
                    "nonce": "nonce",
                    "typedData": typed,
                }
            }
        assert variables == {"signature": "0x" + "1" * 130, "challenge": "nonce"}
        return {"createUserIntentSession": session}

    def sign(args, **kwargs):
        payload = json.loads(Path(args[-1]).read_text())
        assert payload["domain"] == {"name": "Sai", "version": "1"}
        assert payload["message"] == typed["message"]
        return SimpleNamespace(stdout="0x" + "1" * 130)

    monkeypatch.setattr(keeper, "request", query)
    monkeypatch.setattr(keeper.subprocess, "run", sign)
    assert (
        keeper.wallet_session(keeper.MAINNET_HTTP, session["evmAddress"], "wallet")
        == session
    )


def test_market_dry_run_needs_no_price_and_marks_refresh():
    result = CliRunner().invoke(
        main,
        [
            "--json",
            "trade",
            "open",
            "--market",
            "0",
            "--side",
            "long",
            "--type",
            "market",
            "--amount",
            "2",
            "--leverage",
            "2",
            "--slippage",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"requiresMarketPriceRefresh": true' in result.output
    assert "intentPrice" not in result.output
    result = CliRunner().invoke(
        main,
        [
            "trade",
            "open",
            "--market",
            "0",
            "--side",
            "long",
            "--type",
            "limit",
            "--amount",
            "2",
            "--leverage",
            "2",
            "--slippage",
            "1",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    assert "trigger price is required" in result.output


def test_exec_pass_requires_matching_trade_history(session, monkeypatch):
    key = "example"
    address = session["evmAddress"]
    storage.pending(key, keeper.MAINNET_HTTP, address, "CLOSE_POSITION", "1")
    matched = False

    def query(endpoint, query, variables=None, token=None):
        if query == keeper.INTENTS:
            return {
                "userIntents": [
                    {
                        "intentKey": key,
                        "evmAddress": address,
                        "execActor": "server",
                        "lifecycleStep": "exec_pass",
                    }
                ]
            }
        return {
            "perp": {
                "tradeHistory": [{"intentKey": key, "trade": {"id": 1}}]
                if matched
                else []
            }
        }

    monkeypatch.setattr(keeper, "request", query)
    result = CliRunner().invoke(main, ["intent", "status", address, "--key", key])
    assert result.exit_code == 0, result.output
    assert storage.list_pending(keeper.MAINNET_HTTP, address)[0]["status"] == "accepted"
    matched = True
    result = CliRunner().invoke(main, ["intent", "status", address, "--key", key])
    assert result.exit_code == 0, result.output
    assert storage.list_pending(keeper.MAINNET_HTTP, address)[0]["status"] == "terminal"
