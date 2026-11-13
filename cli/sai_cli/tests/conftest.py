"""Prevent in-process tests from connecting to external TCP services."""

from __future__ import annotations

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_tcp(monkeypatch):
    original = socket.socket.connect

    def connect_local_only(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise AssertionError(
                    f"External TCP connection forbidden in tests: {host}"
                )
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect_local_only)
