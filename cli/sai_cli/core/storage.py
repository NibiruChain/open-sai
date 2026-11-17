"""Encrypted sessions and durable, non-secret pending intent identities."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
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


BACKENDS = ("test", "file")

# Backend `test` mirrors the Cosmos SDK `test` keyring: the session is
# encrypted with this fixed key, which ships in the source. It keeps the token
# out of plaintext files, logs, and backups that grep for JWTs. It does not
# protect against anyone who can read the file and this code.
_TEST_KEY = hashlib.sha256(b"sai-cli keyring-backend test; not a secret").digest()


def _key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def _aad(version: int, backend: str) -> bytes:
    return b"sai-session-v1" if version == 1 else f"sai-session-v2-{backend}".encode()


def _session_path() -> Path:
    return directory() / "session.json"


def stored_backend() -> str | None:
    """Return the backend of the stored session, or None if there is none.

    Version-1 envelopes predate backends and always used a passphrase.
    """
    try:
        envelope = json.loads(_session_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("Stored session is unreadable; sign in again") from exc
    fields = envelope if isinstance(envelope, dict) else {}
    if fields.get("version") == 1:
        return "file"
    backend = fields.get("backend")
    if fields.get("version") != 2 or backend not in BACKENDS:
        raise ValueError("Unsupported session version; sign in again")
    return backend


def save_session(session: dict[str, str], password: str | None = None) -> None:
    """Encrypt and store the session.

    With a passphrase, use backend `file` (scrypt-derived key). Without one,
    use backend `test` (fixed key, no prompt).
    """
    backend = "test" if password is None else "file"
    if backend == "file" and not password:
        raise ValueError("Passphrase must not be empty")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _TEST_KEY if password is None else _key(password, salt)
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(session).encode(), _aad(2, backend)
    )
    envelope = {
        "version": 2,
        "backend": backend,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    if backend == "file":
        envelope["salt"] = base64.b64encode(salt).decode()
    root = directory()
    fd, temp = tempfile.mkstemp(dir=root, prefix=".session-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(envelope))
        os.replace(temp, root / "session.json")
    finally:
        Path(temp).unlink(missing_ok=True)


def load_session(password: str | None = None) -> dict[str, str]:
    """Decrypt the stored session with backend `test` (no passphrase) or
    backend `file` (passphrase)."""
    backend = stored_backend()
    if backend is None:
        raise ValueError("No Sai session; run `sai auth import` or `sai auth login`")
    if password is None and backend == "file":
        raise ValueError(
            "Stored session is passphrase-encrypted; pass --keyring-backend file"
            " or sign in again"
        )
    if password is not None and backend == "test":
        raise ValueError(
            "Stored session uses keyring backend test; drop --keyring-backend"
            " file or sign in again"
        )
    try:
        envelope = json.loads(_session_path().read_text(encoding="utf-8"))
        version = envelope["version"]
        nonce, ciphertext = (
            base64.b64decode(envelope[k], validate=True)
            for k in ("nonce", "ciphertext")
        )
        key = (
            _TEST_KEY
            if password is None
            else _key(password, base64.b64decode(envelope["salt"], validate=True))
        )
        session = json.loads(
            AESGCM(key).decrypt(nonce, ciphertext, _aad(version, backend))
        )
        if not isinstance(session, dict) or not all(
            isinstance(session.get(k), str)
            for k in ("token", "evmAddress", "expiresAt")
        ):
            raise ValueError("Invalid session")
        return session
    except (OSError, ValueError, KeyError, InvalidTag) as exc:
        hint = "check the passphrase or " if password is not None else ""
        raise ValueError(f"Cannot unlock session; {hint}sign in again") from exc


# Version 1 adds `created_at`, the Unix time when the CLI reserved the key.
SCHEMA_VERSION = 1


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Intent database was written by a newer sai version")
    if version == SCHEMA_VERSION:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '', created_at REAL)"
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(intents)")
            }
            if "created_at" not in columns:
                connection.execute("ALTER TABLE intents ADD COLUMN created_at REAL")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def db() -> sqlite3.Connection:
    path = directory() / "intents.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=10)
    _migrate(connection)
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
            "INSERT INTO intents (id, endpoint, account, kind, status, entity, created_at) VALUES (?, ?, ?, ?, 'unknown', ?, ?)",
            (key, endpoint, account.lower(), kind, entity, time.time()),
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


def _key_time(key: str) -> float | None:
    """Read the reservation time embedded in an intent key, if it has one."""
    try:
        stamp = datetime.strptime(key[:18], "%Y%m%dT%H%M%S%f")
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC).timestamp()


def mark_absent(
    key: str, endpoint: str, account: str, older_than: float, now: float | None = None
) -> str | None:
    """Mark an unknown intent `absent` once Keeper can no longer admit it.

    Call this only after Keeper returned no record for the key. Returns the
    local status after the check, or None when the key is not recorded here.
    """
    current = time.time() if now is None else now
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, created_at FROM intents WHERE id=? AND endpoint=? AND account=?",
            (key, endpoint, account.lower()),
        ).fetchone()
        if row is None:
            return None
        status, created = row
        created = created if created is not None else _key_time(key)
        if (
            status == "unknown"
            and created is not None
            and current - created > older_than
        ):
            connection.execute(
                "UPDATE intents SET status='absent' WHERE id=? AND endpoint=? AND account=?",
                (key, endpoint, account.lower()),
            )
            return "absent"
        return status
