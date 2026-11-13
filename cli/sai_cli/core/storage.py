"""Encrypted sessions and durable, non-secret pending intent identities."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def home() -> Path:
    return Path(os.environ.get("SAI_HOME", Path.home() / ".sai")) / "trader"


def directory() -> Path:
    path = home()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def save_session(session: dict[str, str], password: str) -> None:
    if not password:
        raise ValueError("Passphrase must not be empty")
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = AESGCM(_key(password, salt)).encrypt(
        nonce, json.dumps(session).encode(), b"sai-session-v1"
    )
    payload = json.dumps(
        {
            "version": 1,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
    )
    root = directory()
    fd, temp = tempfile.mkstemp(dir=root, prefix=".session-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(payload)
        os.replace(temp, root / "session.json")
    finally:
        Path(temp).unlink(missing_ok=True)


def load_session(password: str) -> dict[str, str]:
    try:
        envelope = json.loads(
            (directory() / "session.json").read_text(encoding="utf-8")
        )
        if envelope["version"] != 1:
            raise ValueError("Unsupported session version")
        salt, nonce, ciphertext = (
            base64.b64decode(envelope[k], validate=True)
            for k in ("salt", "nonce", "ciphertext")
        )
        session = json.loads(
            AESGCM(_key(password, salt)).decrypt(nonce, ciphertext, b"sai-session-v1")
        )
        if not isinstance(session, dict) or not all(
            isinstance(session.get(k), str)
            for k in ("token", "evmAddress", "expiresAt")
        ):
            raise ValueError("Invalid session")
        return session
    except (OSError, ValueError, KeyError, InvalidTag) as exc:
        raise ValueError(
            "Cannot unlock session; check the passphrase or sign in again"
        ) from exc


def db() -> sqlite3.Connection:
    path = directory() / "intents.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '')"
    )
    return connection


def pending(key: str, endpoint: str, account: str, kind: str, entity: str) -> None:
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        unresolved = connection.execute(
            "SELECT id FROM intents WHERE endpoint=? AND account=? AND (status='unknown' OR (status='accepted' AND entity <> '' AND entity=?)) LIMIT 1",
            (endpoint, account.lower(), entity),
        ).fetchone()
        if unresolved:
            raise ValueError(
                f"Reconcile uncertain intent {unresolved[0]} before submitting another trade"
            )
        connection.execute(
            "INSERT INTO intents VALUES (?, ?, ?, ?, 'unknown', ?)",
            (key, endpoint, account.lower(), kind, entity),
        )


def set_status(key: str, endpoint: str, account: str, status: str) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE intents SET status=? WHERE id=? AND endpoint=? AND account=?",
            (status, key, endpoint, account.lower()),
        )


def list_pending(endpoint: str, account: str) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            "SELECT id,kind,status FROM intents WHERE endpoint=? AND account=? ORDER BY rowid DESC",
            (endpoint, account.lower()),
        ).fetchall()
    return [{"intentKey": row[0], "kind": row[1], "status": row[2]} for row in rows]
