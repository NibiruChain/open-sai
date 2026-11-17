"""Connect the installed CLI through the clipboard, a terminal, and cast.

Each test runs the CLI as a subprocess with a temp SAI_HOME and a PATH that
holds only fake clipboard and cast programs, so no real clipboard or keystore
is touched.
"""

from __future__ import annotations

import base64
import json
import os
import pty
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sai_cli.core import keeper, storage

ADDRESS = "0x" + "b" * 40
OTHER = "0x" + "c" * 40
# Keeper sends the nonce as bare hex and the typed-data challenge with 0x.
NONCE = "12" * 32
SIGNATURE = "0x" + "1" * 130


def make_token(address: str = ADDRESS, hours: int = 720) -> str:
    def part(value: dict) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    exp = int((datetime.now(UTC) + timedelta(hours=hours)).timestamp())
    claims = {"purpose": "user_intent", "sub": address, "exp": exp}
    return f"{part({'alg': 'HS256'})}.{part(claims)}.c2lnbmF0dXJl"


class Env:
    """A temp SAI_HOME, a fake clipboard file, and a PATH of fake tools."""

    def __init__(self, root: Path) -> None:
        self.home = root / "sai-home"
        self.bin = root / "bin"
        self.clip = root / "clipboard.txt"
        self.log = root / "calls.log"
        self.bin.mkdir()
        self.log.touch()

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"CLIP = {str(self.clip)!r}\n"
            f"open({str(self.log)!r}, 'a').write(' '.join(sys.argv) + '\\n')\n" + body,
            encoding="utf-8",
        )
        path.chmod(0o755)

    def clipboard(self, reader: str = "pbpaste", writer: str = "pbcopy") -> None:
        self.tool(reader, "sys.stdout.write(open(CLIP).read())\n")
        self.tool(writer, "open(CLIP, 'w').write(sys.stdin.read())\n")

    def vars(self) -> dict[str, str]:
        # Built from scratch so no credentials from the parent shell leak in.
        return {
            "SAI_HOME": str(self.home),
            "PATH": str(self.bin),
            "HOME": str(self.home),
        }

    def run(
        self, *args: str, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sai_cli", *args],
            input=stdin,
            check=False,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.vars(),
            timeout=30,
        )

    def run_tty(
        self, *args: str, typed: list[str | Callable[[], object]]
    ) -> tuple[int, str, str]:
        """Run with a pseudo-terminal on stdin and answer each hidden prompt.

        `getpass` turns echo off with TCSAFLUSH, which drops input typed
        earlier. Each line therefore waits until stderr has grown since the
        previous answer, which `getpass` only does after reading it, and echo
        is off again for the next prompt. A callable entry runs once that
        next prompt is showing, before the line that answers it.
        """
        primary, secondary = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-m", "sai_cli", *args],
            stdin=secondary,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.vars(),
            start_new_session=True,
        )
        os.close(secondary)
        errors = bytearray()

        def drain() -> None:
            assert proc.stderr
            while chunk := os.read(proc.stderr.fileno(), 4096):
                errors.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            mark = 0
            for line in typed:
                deadline = time.monotonic() + 20
                while proc.poll() is None and time.monotonic() < deadline:
                    echo = termios.tcgetattr(primary)[3] & termios.ECHO
                    if len(errors) > mark and not echo:
                        break
                    time.sleep(0.01)
                if callable(line):
                    line()
                    continue
                os.write(primary, (line + "\n").encode())
                mark = len(errors)
            assert proc.stdout
            stdout = proc.stdout.read()
            proc.wait(timeout=30)
            reader.join(timeout=5)
        finally:
            proc.kill()
            os.close(primary)
        return proc.returncode, stdout.decode(), errors.decode()

    def stored_bytes(self) -> bytes:
        return b"".join(
            path.read_bytes() for path in self.home.rglob("*") if path.is_file()
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def assert_connected(env: Env, token: str, result_stdout: str) -> None:
    output = json.loads(result_stdout)
    assert output == {
        "address": ADDRESS,
        "expiresAt": output["expiresAt"],
        "endpoint": keeper.MAINNET_HTTP,
    }
    assert token.encode() not in env.stored_bytes()
    status = env.run("--json", "auth", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["address"] == ADDRESS
    assert token not in status.stdout + status.stderr


def test_agent_import_reads_and_clears_clipboard(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token + "\n")
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout + result.stderr
    assert env.clip.read_text() == ""
    assert_connected(env, token, result.stdout)


def test_agent_import_ignores_piped_stdin(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    piped = make_token(OTHER)
    result = env.run("--json", "auth", "import", stdin=piped + "\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["address"] == ADDRESS
    assert piped not in result.stdout + result.stderr


def test_agent_import_skips_failing_clipboard_tool(env: Env) -> None:
    token = make_token()
    env.tool("wl-paste", "sys.exit(1)\n")
    # One fake xclip both reads (-o) and writes (stdin), as the real one does.
    env.tool(
        "xclip",
        "if '-o' in sys.argv:\n"
        "    sys.stdout.write(open(CLIP).read())\n"
        "else:\n"
        "    open(CLIP, 'w').write(sys.stdin.read())\n",
    )
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    assert env.clip.read_text() == ""
    calls = env.log.read_text()
    assert "wl-paste --no-newline" in calls
    assert "xclip -selection clipboard -o" in calls


def test_agent_import_skips_binary_clipboard(env: Env) -> None:
    token = make_token()
    env.tool("pbpaste", "sys.stdout.buffer.write(b'\\x89PNG\\r\\n\\x1a\\n\\xff')\n")
    env.tool(
        "xclip",
        "if '-o' in sys.argv:\n"
        "    sys.stdout.write(open(CLIP).read())\n"
        "else:\n"
        "    open(CLIP, 'w').write(sys.stdin.read())\n",
    )
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    assert "codec" not in result.stderr
    assert env.clip.read_text() == ""


def test_agent_import_without_clipboard_asks_for_a_terminal(env: Env) -> None:
    result = env.run("auth", "import")
    assert result.returncode != 0
    assert "Ask the person to run `sai auth import`" in result.stderr
    assert not (env.home / "trader" / "session.json").exists()


def test_invalid_clipboard_is_not_echoed_or_cleared(env: Env) -> None:
    secret = "not-a-jwt-but-maybe-a-password"
    expired = make_token(hours=-1)
    env.clipboard()
    for content in (secret, expired):
        env.clip.write_text(content)
        result = env.run("auth", "import")
        assert result.returncode != 0
        assert "Invalid or expired Sai session token" in result.stderr
        assert content not in result.stdout + result.stderr
        assert env.clip.read_text() == content


def test_terminal_import_reads_hidden_prompt(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text("unrelated clipboard text")
    code, stdout, stderr = env.run_tty("--json", "auth", "import", typed=[token])
    assert code == 0, stderr
    assert token not in stdout + stderr
    # The clipboard held something else, so the CLI leaves it alone.
    assert env.clip.read_text() == "unrelated clipboard text"
    assert_connected(env, token, stdout)


def test_terminal_import_clears_pasted_token(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    code, _, stderr = env.run_tty("--json", "auth", "import", typed=[token])
    assert code == 0, stderr
    assert env.clip.read_text() == ""


def test_terminal_import_enter_reads_clipboard(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    code, stdout, stderr = env.run_tty("--json", "auth", "import", typed=[""])
    assert code == 0, stderr
    assert token not in stdout + stderr
    assert env.clip.read_text() == ""
    assert_connected(env, token, stdout)


def test_file_backend_import_and_status_use_passphrase(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    flags = ("--json", "--keyring-backend", "file")
    code, stdout, stderr = env.run_tty(
        *flags, "auth", "import", typed=["", "hunter22", "hunter22"]
    )
    assert code == 0, stderr
    envelope = json.loads((env.home / "trader" / "session.json").read_text())
    assert (envelope["version"], envelope["backend"]) == (2, "file")
    # Copying a passphrase during the prompts must survive the clear.
    env.clip.write_text(token)
    code, _, stderr = env.run_tty(
        *flags,
        "auth",
        "import",
        typed=["", lambda: env.clip.write_text("hunter22"), "hunter22", "hunter22"],
    )
    assert code == 0, stderr
    assert env.clip.read_text() == "hunter22"
    code, stdout, stderr = env.run_tty(*flags, "auth", "status", typed=["hunter22"])
    assert code == 0, stderr
    assert json.loads(stdout)["address"] == ADDRESS
    default = env.run("auth", "status")
    assert default.returncode != 0
    assert "--keyring-backend file" in default.stderr


def test_keyring_backend_env_var(env: Env) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sai_cli", "auth", "status"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        env={**env.vars(), "SAI_KEYRING_BACKEND": "nope"},
        timeout=30,
    )
    assert result.returncode == 2
    assert "'test', 'file'" in result.stderr or "test, file" in result.stderr


def write_v1_session(home: Path, session: dict[str, str], password: str) -> None:
    """Write the envelope format that shipped before keyring backends."""
    salt, nonce = os.urandom(16), os.urandom(12)
    key = storage._key(password, salt)
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(session).encode(), b"sai-session-v1"
    )
    root = home / "trader"
    root.mkdir(parents=True)
    (root / "session.json").write_text(
        json.dumps(
            {
                "version": 1,
                "salt": base64.b64encode(salt).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(ciphertext).decode(),
            }
        )
    )


def test_version_1_session_loads_under_file_backend(env: Env) -> None:
    token = make_token()
    session = {
        "token": token,
        "evmAddress": ADDRESS,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "endpoint": keeper.MAINNET_HTTP,
    }
    write_v1_session(env.home, session, "old passphrase")
    under_test = env.run("auth", "status")
    assert under_test.returncode != 0
    assert "passphrase-encrypted; pass --keyring-backend file" in under_test.stderr
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"old passphrase\n")
    os.close(write_fd)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "sai_cli", "--json", "--keyring-backend", "file"]
            + ["--passphrase-fd", str(read_fd), "auth", "status"],
            pass_fds=(read_fd,),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env.vars(),
            timeout=30,
        )
    finally:
        os.close(read_fd)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["address"] == ADDRESS
    assert token not in result.stdout + result.stderr


def test_test_backend_envelope_is_bound_to_its_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SAI_HOME", str(tmp_path))
    session = {"token": "t", "evmAddress": ADDRESS, "expiresAt": "x"}
    storage.save_session(session)
    path = storage.home() / "session.json"
    envelope = json.loads(path.read_text())
    assert (envelope["version"], envelope["backend"]) == (2, "test")
    assert storage.load_session() == session
    envelope["backend"] = "file"
    envelope["salt"] = base64.b64encode(b"0" * 16).decode()
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="Cannot unlock"):
        storage.load_session("anything")


def typed_data(**overrides) -> dict:
    data: dict[str, Any] = {
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
        "domain": {"name": "Sai", "version": "1", "chainId": 6900},
        "message": {
            "account": ADDRESS,
            "challenge": "0x" + NONCE,
            "action": "Enable Sai trading",
            "expiresAt": "2026-11-17T00:05:00Z",
        },
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            data[section][field] = value
        else:
            data[section] = value
    return data


@pytest.mark.parametrize(
    "overrides",
    [
        {"primaryType": "Permit"},
        {"domain__name": "Uniswap"},
        {"domain__version": "2"},
        {"message__account": OTHER},
        {"message__challenge": "0x" + "34" * 32},
        {"message": {}},
    ],
)
def test_check_session_typed_data_rejects_other_requests(overrides) -> None:
    with pytest.raises(keeper.KeeperError, match="not a Sai trading session"):
        keeper.check_session_typed_data(typed_data(**overrides), ADDRESS, NONCE)


def test_check_session_typed_data_accepts_keeper_challenge() -> None:
    keeper.check_session_typed_data(typed_data(), ADDRESS.upper(), NONCE.upper())


@pytest.fixture
def keeper_auth():
    """Serve Keeper's challenge and session mutations on loopback."""
    state: dict[str, Any] = {
        "typed": typed_data(),
        "token": make_token(),
        "signatures": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "createUserIntentAuthChallenge" in body["query"]:
                data = {
                    "createUserIntentAuthChallenge": {
                        "nonce": NONCE,
                        "evmAddress": body["variables"]["address"],
                        "expiresAt": "2026-11-17T00:05:00Z",
                        "chainId": 6900,
                        "typedData": state["typed"],
                    }
                }
            else:
                state["signatures"].append(body["variables"])
                data = {
                    "createUserIntentSession": {
                        "token": state["token"],
                        "evmAddress": ADDRESS,
                        "expiresAt": "2026-12-17T00:00:00+00:00",
                    }
                }
            encoded = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["endpoint"] = f"http://127.0.0.1:{server.server_port}/query"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def fake_cast(env: Env) -> None:
    env.tool(
        "cast",
        "import json\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['wallet', 'address']:\n"
        f"    print({ADDRESS!r})\n"
        "elif args[:2] == ['wallet', 'sign']:\n"
        "    data = json.load(open(args[-1]))\n"
        "    assert data['primaryType'] == 'UserTradingSession'\n"
        f"    print({SIGNATURE!r})\n"
        "else:\n"
        "    sys.exit(2)\n",
    )


def test_cast_login_derives_address_and_signs(env: Env, keeper_auth) -> None:
    fake_cast(env)
    result = env.run(
        "--json",
        "--endpoint",
        keeper_auth["endpoint"],
        "auth",
        "login",
        "--account",
        "dev",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["address"] == ADDRESS
    assert keeper_auth["token"] not in result.stdout + result.stderr
    calls = env.log.read_text().splitlines()
    assert calls[0].endswith("cast wallet address --account dev")
    assert " wallet sign --account dev --data --from-file " in calls[1]
    assert keeper_auth["signatures"] == [{"signature": SIGNATURE, "challenge": NONCE}]
    status = env.run("--json", "--endpoint", keeper_auth["endpoint"], "auth", "status")
    assert status.returncode == 0, status.stderr


def test_cast_login_refuses_foreign_typed_data(env: Env, keeper_auth) -> None:
    fake_cast(env)
    keeper_auth["typed"] = typed_data(primaryType="Permit")
    result = env.run(
        "--endpoint",
        keeper_auth["endpoint"],
        "auth",
        "login",
        "--account",
        "dev",
        "--address",
        ADDRESS,
    )
    assert result.returncode != 0
    assert "not a Sai trading session request" in result.stderr
    assert env.log.read_text() == ""
    assert keeper_auth["signatures"] == []


def test_login_requires_one_signer(env: Env) -> None:
    neither = env.run("auth", "login")
    both = env.run("auth", "login", "--account", "a", "--wallet", "b")
    wallet_only = env.run("auth", "login", "--wallet", "b")
    for result in (neither, both):
        assert result.returncode == 2
        assert "exactly one of --account or --wallet" in result.stderr
    assert wallet_only.returncode == 2
    assert "--wallet requires --address" in wallet_only.stderr
