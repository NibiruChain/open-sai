"""Refuse wallet signing for anything but Keeper's session challenge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sai_cli.core import keeper

ADDR = "0x" + "a" * 40


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


# Keeper returns the nonce as bare hex and signs it as a 0x-prefixed bytes32
# (`userintent/challenge.go` and `journal_session_typed_data.go`).
NONCE = "ab" * 32


def challenge() -> dict:
    """Typed data shaped exactly like Keeper's UserTradingSessionTypedData."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "UserTradingSession": [
                {"name": "account", "type": "address"},
                {"name": "challenge", "type": "bytes32"},
                {"name": "action", "type": "string"},
                {"name": "expiresAt", "type": "string"},
            ],
        },
        "primaryType": "UserTradingSession",
        "domain": {"name": "Sai", "version": "1", "chainId": "0x1b58"},
        "message": {
            "account": "0x" + "A" * 40,
            "challenge": "0x" + NONCE,
            "action": "Enable Sai trading",
            "expiresAt": "2099-01-01T00:05:00Z",
        },
    }


def test_session_typed_data_accepts_keeper_challenge():
    keeper.check_session_typed_data(challenge(), ADDR, NONCE)
    keeper.check_session_typed_data(challenge(), ADDR, NONCE.upper())


@pytest.mark.parametrize(
    "path, value",
    [
        (("primaryType",), "Permit"),
        (("domain", "name"), "Uniswap"),
        (("domain", "version"), "2"),
        (("message", "account"), "0x" + "b" * 40),
        (("message", "challenge"), "0x" + "cd" * 32),
        (("message", "challenge"), "0x" + NONCE[:-2]),
        (("message", "challenge"), 7),
    ],
)
def test_session_typed_data_refuses_substitution(path, value):
    data = challenge()
    target = data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        keeper.check_session_typed_data(data, ADDR, NONCE)


@pytest.mark.parametrize("nonce", ["", NONCE[:-2], "zz" * 32, NONCE + "00"])
def test_session_typed_data_refuses_malformed_nonce(nonce):
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        keeper.check_session_typed_data(challenge(), ADDR, nonce)


def test_session_typed_data_refuses_missing_message():
    data = challenge()
    del data["message"]
    with pytest.raises(keeper.KeeperError):
        keeper.check_session_typed_data(data, ADDR, NONCE)


def wallet_login(monkeypatch, data: dict) -> list:
    signed = []

    def query(endpoint, query, variables=None, token=None):
        if query == keeper.CHALLENGE:
            return {
                "createUserIntentAuthChallenge": {
                    "evmAddress": ADDR,
                    "nonce": NONCE,
                    "typedData": data,
                }
            }
        assert query == keeper.SESSION
        assert variables == {"signature": "0x" + "1" * 130, "challenge": NONCE}
        return {"createUserIntentSession": {"evmAddress": ADDR}}

    def sign(args, **kwargs):
        signed.append(args)
        return SimpleNamespace(stdout="0x" + "1" * 130)

    monkeypatch.setattr(keeper, "request", query)
    monkeypatch.setattr(keeper.subprocess, "run", sign)
    keeper.wallet_session(keeper.MAINNET_HTTP, ADDR, "wallet")
    return signed


def test_wallet_login_signs_a_keeper_shaped_challenge(monkeypatch):
    assert len(wallet_login(monkeypatch, challenge())) == 1


def test_wallet_login_never_signs_a_substituted_payload(monkeypatch):
    data = challenge()
    data["primaryType"] = "Permit"
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        wallet_login(monkeypatch, data)
