"""Encrypted sessions for connected accounts and durable, non-secret pending
intent identities."""

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


def home() -> Path:
    return Path(os.environ.get("SAI_HOME", Path.home() / ".sai")) / "trader"


def directory() -> Path:
    path = home()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


# Sessions are encrypted with this fixed key, which ships in the source, like
# the Cosmos SDK `test` keyring. It keeps tokens out of plaintext files, logs,
# and backups that grep for JWTs. It does not protect against anyone who can
# read the file and this code; the file mode (0600) does that part.
_SESSION_KEY = hashlib.sha256(b"sai-cli keyring-backend test; not a secret").digest()
# Version 3 holds every connected account and which one is active.
_VERSION = 3
_AAD = b"sai-sessions-v3"
# Version 2 held one session; it is read once and rewritten as version 3.
_V2_AAD = b"sai-session-v2-test"

SIGN_IN_AGAIN = "run `sai auth import` or `sai auth login` again"
NO_SESSION = "No Sai session; run `sai auth import` or `sai auth login`"
SESSION_FIELDS = ("token", "evmAddress", "expiresAt")


def _sessions_path() -> Path:
    return directory() / "sessions.json"


def _v2_path() -> Path:
    return home() / "session.json"


def _decrypt(fields: dict[str, Any], aad: bytes) -> Any:
    nonce, ciphertext = (
        base64.b64decode(fields[k], validate=True) for k in ("nonce", "ciphertext")
    )
    return json.loads(AESGCM(_SESSION_KEY).decrypt(nonce, ciphertext, aad))


def _check_session(session: Any) -> dict[str, str]:
    if not isinstance(session, dict) or not all(
        isinstance(session.get(k), str) for k in SESSION_FIELDS
    ):
        raise ValueError("Invalid session")
    return session


def _read_envelope(path: Path) -> dict[str, Any] | None:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Stored session is unreadable; {SIGN_IN_AGAIN}") from exc
    return envelope if isinstance(envelope, dict) else {}


def _load_v2(fields: dict[str, Any]) -> dict[str, Any]:
    """Read a version 2 single-session file as a version 3 account set.

    Sessions from versions that used a passphrase (envelope version 1, or
    version 2 with backend `file`) are refused, never prompted for.
    """
    if fields.get("version") == 1 or (
        fields.get("version") == 2 and fields.get("backend") == "file"
    ):
        raise ValueError(
            "Stored session is passphrase-encrypted, which sai no longer"
            f" supports; {SIGN_IN_AGAIN}"
        )
    if fields.get("version") != 2 or fields.get("backend") != "test":
        raise ValueError(f"Unsupported session version; {SIGN_IN_AGAIN}")
    try:
        session = _check_session(_decrypt(fields, _V2_AAD))
    except (ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise ValueError(f"Cannot unlock session; {SIGN_IN_AGAIN}") from exc
    address = session["evmAddress"].lower()
    return {"active": address, "sessions": {address: session}}


def load_accounts() -> dict[str, Any]:
    """Decrypt every connected account.

    Returns {"active": address or None, "sessions": {address: session}}, with
    addresses in lowercase. An empty set means nobody is signed in. A version
    2 `session.json` is read in place; the next save rewrites it.
    """
    fields = _read_envelope(_sessions_path())
    if fields is None:
        old = _read_envelope(_v2_path())
        return {"active": None, "sessions": {}} if old is None else _load_v2(old)
    if fields.get("version") != _VERSION:
        raise ValueError(f"Unsupported session version; {SIGN_IN_AGAIN}")
    try:
        accounts = _decrypt(fields, _AAD)
        stored = accounts["sessions"]
        if not isinstance(stored, dict):
            raise TypeError("Invalid session set")
        sessions = {k: _check_session(v) for k, v in stored.items()}
        active = accounts.get("active")
        if active is not None and active not in sessions:
            active = None
    except (ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise ValueError(f"Cannot unlock session; {SIGN_IN_AGAIN}") from exc
    return {"active": active, "sessions": sessions}


def _save_accounts(accounts: dict[str, Any]) -> None:
    """Encrypt ACCOUNTS and store them at mode 0600; drop the version 2 file."""
    nonce = os.urandom(12)
    plaintext = json.dumps(accounts).encode()
    ciphertext = AESGCM(_SESSION_KEY).encrypt(nonce, plaintext, _AAD)
    envelope = {
        "version": _VERSION,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    root = directory()
    # mkstemp creates the file with mode 0600.
    fd, temp = tempfile.mkstemp(dir=root, prefix=".sessions-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(envelope))
        os.replace(temp, _sessions_path())
    finally:
        Path(temp).unlink(missing_ok=True)
    _v2_path().unlink(missing_ok=True)


def _find(accounts: dict[str, Any], address: str) -> str:
    key = address.lower()
    if key not in accounts["sessions"]:
        raise ValueError(
            f"{address} is not connected; run `sai auth list` to see connected"
            " accounts, or sign in with `sai auth login` or `sai auth import`"
        )
    return key


def save_session(session: dict[str, str]) -> None:
    """Add or replace the account for SESSION's address and make it active."""
    accounts = load_accounts()
    address = session["evmAddress"].lower()
    accounts["sessions"][address] = session
    accounts["active"] = address
    _save_accounts(accounts)


def load_session(address: str | None = None) -> dict[str, str]:
    """Return the session for ADDRESS, or the active account's session."""
    accounts = load_accounts()
    if address is not None:
        return accounts["sessions"][_find(accounts, address)]
    if accounts["active"] is None:
        if accounts["sessions"]:
            raise ValueError(
                "No active Sai account; run `sai auth use ADDRESS` with an"
                " address from `sai auth list`"
            )
        raise ValueError(NO_SESSION)
    return accounts["sessions"][accounts["active"]]


def use_session(address: str) -> dict[str, str]:
    """Make the connected ADDRESS the active account and return its session."""
    accounts = load_accounts()
    key = _find(accounts, address)
    accounts["active"] = key
    _save_accounts(accounts)
    return accounts["sessions"][key]


def session_exists() -> bool:
    """Report whether any session file is stored, without reading it."""
    return (home() / "sessions.json").is_file() or _v2_path().is_file()


def delete_session(address: str | None = None) -> str | None:
    """Remove ADDRESS, or the active account, from this machine.

    Returns the removed address, or None when there was nothing to remove.
    Removing the active account leaves no account active.
    """
    if not session_exists():
        return None
    accounts = load_accounts()
    if address is None:
        key = accounts["active"]
        if key is None:
            return None
    else:
        key = address.lower()
        if key not in accounts["sessions"]:
            return None
    removed = accounts["sessions"].pop(key)["evmAddress"]
    if accounts["active"] == key:
        accounts["active"] = None
    if accounts["sessions"]:
        _save_accounts(accounts)
    else:
        _sessions_path().unlink(missing_ok=True)
        _v2_path().unlink(missing_ok=True)
    return removed


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
