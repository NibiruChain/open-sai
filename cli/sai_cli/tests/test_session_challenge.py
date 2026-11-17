"""Refuse wallet signing for anything but Keeper's session challenge."""

from __future__ import annotations

import pytest
from sai_cli.core import keeper

ADDRESS = "0x" + "a" * 40


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_HOME", str(tmp_path))


def challenge(nonce: str = "0x" + "ab" * 32) -> dict:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
            ],
            "UserTradingSession": [
                {"name": "account", "type": "address"},
                {"name": "challenge", "type": "bytes32"},
            ],
        },
        "primaryType": "UserTradingSession",
        "domain": {"name": "Sai", "version": "1"},
        "message": {"account": ADDRESS.upper().replace("0X", "0x"), "challenge": nonce},
    }


def test_session_typed_data_accepts_keeper_challenge():
    keeper.check_session_typed_data(challenge(), ADDRESS, "0x" + "AB" * 32)


@pytest.mark.parametrize(
    "path, value",
    [
        (("primaryType",), "Permit"),
        (("domain", "name"), "Uniswap"),
        (("domain", "version"), "2"),
        (("message", "account"), "0x" + "b" * 40),
        (("message", "challenge"), "0x" + "cd" * 32),
    ],
)
def test_session_typed_data_refuses_substitution(path, value):
    data = challenge()
    target = data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        keeper.check_session_typed_data(data, ADDRESS, "0x" + "ab" * 32)


def test_session_typed_data_refuses_missing_message():
    data = challenge()
    del data["message"]
    with pytest.raises(keeper.KeeperError):
        keeper.check_session_typed_data(data, ADDRESS, "0x" + "ab" * 32)


def test_wallet_login_never_signs_a_substituted_payload(monkeypatch):
    data = challenge()
    data["primaryType"] = "Permit"

    def query(endpoint, query, variables=None, token=None):
        assert query == keeper.CHALLENGE
        return {
            "createUserIntentAuthChallenge": {
                "evmAddress": ADDRESS,
                "nonce": "0x" + "ab" * 32,
                "typedData": data,
            }
        }

    def sign(*args, **kwargs):
        raise AssertionError("signer must not run")

    monkeypatch.setattr(keeper, "request", query)
    monkeypatch.setattr(keeper.subprocess, "run", sign)
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        keeper.wallet_session(keeper.MAINNET_HTTP, ADDRESS, "wallet")
