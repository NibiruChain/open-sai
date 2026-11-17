"""Connect the installed CLI through the clipboard, a terminal, and cast.

Each test runs the CLI as a subprocess with a temp SAI_HOME and a PATH that
holds only fake clipboard and cast programs, so no real clipboard or keystore
is touched.
"""

from __future__ import annotations

import base64
import hashlib
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
from sai_cli.core import keeper

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


def test_logout_deletes_the_session_without_network(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    assert env.run("auth", "import").returncode == 0
    # An endpoint nothing listens on: logout must not contact Keeper.
    result = env.run("--json", "--endpoint", "http://127.0.0.1:9/q", "auth", "logout")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["removed"] is True
    assert "cannot revoke" in output["note"]
    assert token not in result.stdout + result.stderr
    assert not (env.home / "trader" / "session.json").exists()
    status = env.run("auth", "status")
    assert status.returncode != 0
    assert "No Sai session" in status.stderr
    again = env.run("--json", "auth", "logout")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["removed"] is False


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


def session_for(token: str) -> dict[str, str]:
    return {
        "token": token,
        "evmAddress": ADDRESS,
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "endpoint": keeper.MAINNET_HTTP,
    }


def write_envelope(home: Path, envelope: dict[str, Any]) -> None:
    root = home / "trader"
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.json").write_text(json.dumps(envelope))


def random_b64(size: int) -> str:
    return base64.b64encode(os.urandom(size)).decode()


@pytest.mark.parametrize(
    "envelope",
    [
        # Shipped before keyring backends: always passphrase-encrypted.
        {"version": 1, "salt": random_b64(16)},
        # Keyring backend `file`, removed with the session passphrase.
        {"version": 2, "backend": "file", "salt": random_b64(16)},
    ],
)
def test_passphrase_session_asks_to_sign_in_again(env: Env, envelope) -> None:
    envelope |= {"nonce": random_b64(12), "ciphertext": random_b64(64)}
    write_envelope(env.home, envelope)
    # A terminal on stdin proves the CLI fails instead of prompting.
    code, stdout, stderr = env.run_tty("auth", "status", typed=[])
    assert code != 0
    assert "passphrase-encrypted" in stderr
    assert "run `sai auth import` or `sai auth login` again" in stderr
    assert "assphrase:" not in stderr
    assert stdout == ""


def test_removed_passphrase_options_are_rejected(env: Env) -> None:
    for flags in (["--keyring-backend", "file"], ["--passphrase-fd", "0"]):
        result = env.run(*flags, "auth", "status")
        assert result.returncode == 2
        assert "No such option" in result.stderr


def test_session_envelope_uses_the_built_in_key(env: Env) -> None:
    token = make_token()
    env.clipboard()
    env.clip.write_text(token)
    result = env.run("--json", "auth", "import")
    assert result.returncode == 0, result.stderr
    path = env.home / "trader" / "session.json"
    assert path.stat().st_mode & 0o777 == 0o600
    envelope = json.loads(path.read_text())
    assert set(envelope) == {"version", "backend", "nonce", "ciphertext"}
    assert (envelope["version"], envelope["backend"]) == (2, "test")
    # Sessions written by the old default backend `test` still load.
    nonce = os.urandom(12)
    key = hashlib.sha256(b"sai-cli keyring-backend test; not a secret").digest()
    session = session_for(token)
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(session).encode(), b"sai-session-v2-test"
    )
    envelope["nonce"] = base64.b64encode(nonce).decode()
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode()
    path.write_text(json.dumps(envelope))
    status = env.run("--json", "auth", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["address"] == ADDRESS


def test_tampered_session_cannot_be_unlocked(env: Env) -> None:
    write_envelope(
        env.home,
        {
            "version": 2,
            "backend": "test",
            "nonce": random_b64(12),
            "ciphertext": random_b64(64),
        },
    )
    result = env.run("auth", "status")
    assert result.returncode != 0
    assert "Cannot unlock session; run `sai auth import`" in result.stderr


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
    for result in (neither, both):
        assert result.returncode == 2
        assert "exactly one of --account or --wallet" in result.stderr


# Stand-in for a value that ud keeps in `pass` and must never reach output.
UD_SECRET = "ud-keystore-password"


def fake_ud(env: Env, known: str = "dev", state: str = "matching") -> None:
    """A ud that knows wallet KNOWN, whose Cast keystore is in STATE.

    `show` prints ud's real JSON shape, plus a log line and a secret on
    stderr, as a noisy real tool might. Like real ud, `run` signs only when
    the state is `matching`.
    """
    shown = {
        "name": known,
        "address": ADDRESS.upper().replace("0X", "0x"),
        "notes": "",
        "cast": {
            "name": known,
            "address": ADDRESS.upper().replace("0X", "0x"),
            "state": state,
            "keystorePath": f"/keystores/{known}",
            "duplicateNames": [],
        },
    }
    env.tool(
        "ud",
        "import json\n"
        "args = sys.argv[1:]\n"
        f"if args[:3] == ['evm', 'wallet', 'show'] and args[3] == {known!r}:\n"
        f"    sys.stderr.write({UD_SECRET!r} + '\\n')\n"
        "    print('Reading wallet document')\n"
        f"    print(json.dumps({shown!r}))\n"
        f"elif args[:4] == ['evm', 'wallet', 'run', {known!r}] and args[4:7] == "
        "['--', 'wallet', 'sign']:\n"
        f"    if {state!r} != 'matching':\n"
        "        sys.exit('wallet is not synchronized with Cast')\n"
        "    assert args[7:9] == ['--data', '--from-file']\n"
        "    data = json.load(open(args[-1]))\n"
        "    assert data['primaryType'] == 'UserTradingSession'\n"
        f"    print({SIGNATURE!r})\n"
        "else:\n"
        "    sys.exit(1)\n",
    )


def login(env: Env, keeper_auth, *args: str) -> subprocess.CompletedProcess[str]:
    return env.run(
        "--json", "--endpoint", keeper_auth["endpoint"], "auth", "login", *args
    )


def test_wallet_login_reads_address_from_ud(env: Env, keeper_auth) -> None:
    fake_ud(env)
    result = login(env, keeper_auth, "--wallet", "dev")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["address"] == ADDRESS
    assert UD_SECRET not in result.stdout + result.stderr
    assert keeper_auth["token"] not in result.stdout + result.stderr
    calls = env.log.read_text().splitlines()
    assert calls[0].endswith("ud evm wallet show dev")
    assert " evm wallet run dev -- wallet sign --data --from-file " in calls[1]
    assert keeper_auth["signatures"] == [{"signature": SIGNATURE, "challenge": NONCE}]
    status = env.run("--json", "--endpoint", keeper_auth["endpoint"], "auth", "status")
    assert status.returncode == 0, status.stderr


def test_wallet_login_cross_checks_address(env: Env, keeper_auth) -> None:
    fake_ud(env)
    same = login(env, keeper_auth, "--wallet", "dev", "--address", ADDRESS.upper())
    assert same.returncode == 0, same.stderr
    env.log.write_text("")
    other = login(env, keeper_auth, "--wallet", "dev", "--address", OTHER)
    assert other.returncode != 0
    assert "does not match the wallet address" in other.stderr
    assert "wallet run" not in env.log.read_text()
    assert len(keeper_auth["signatures"]) == 1


def test_wallet_login_fails_when_ud_does_not_know_the_wallet(
    env: Env, keeper_auth
) -> None:
    fake_ud(env)
    fake_cast(env)
    result = login(env, keeper_auth, "--wallet", "other")
    assert result.returncode != 0
    assert "`ud evm wallet show other` failed" in result.stderr
    assert "cast" not in env.log.read_text()
    assert keeper_auth["signatures"] == []


def test_account_login_never_consults_ud(env: Env, keeper_auth) -> None:
    # ud is installed and knows a matching wallet `dev`; --account still means
    # the Foundry keystore `dev` and signs with cast alone.
    fake_ud(env)
    fake_cast(env)
    result = login(env, keeper_auth, "--account", "dev")
    assert result.returncode == 0, result.stderr
    calls = env.log.read_text().splitlines()
    assert len(calls) == 2
    assert calls[0].endswith("cast wallet address --account dev")
    assert " wallet sign --account dev --data --from-file " in calls[1]
    assert "/ud " not in env.log.read_text()
    assert len(keeper_auth["signatures"]) == 1


def test_wallet_login_without_ud_points_to_public_paths(env: Env, keeper_auth) -> None:
    fake_cast(env)
    result = login(env, keeper_auth, "--wallet", "dev")
    assert result.returncode != 0
    assert "--wallet needs the `ud` tool" in result.stderr
    assert "--account NAME" in result.stderr
    assert env.log.read_text() == ""
    assert keeper_auth["signatures"] == []


@pytest.mark.parametrize(
    "state, hint",
    [
        ("missing", "run `ud evm wallet sync dev` to create it"),
        ("name-conflict", "another keystore already uses that name"),
    ],
)
def test_wallet_login_names_an_unsigned_cast_state(
    env: Env, keeper_auth, state: str, hint: str
) -> None:
    fake_ud(env, state=state)
    fake_cast(env)
    result = login(env, keeper_auth, "--wallet", "dev")
    assert result.returncode != 0
    assert f"Cast keystore state {state}" in result.stderr
    assert hint in result.stderr
    calls = env.log.read_text()
    assert "wallet run" not in calls
    assert "/cast " not in calls
    assert keeper_auth["signatures"] == []


def test_ud_login_refuses_foreign_typed_data(env: Env, keeper_auth) -> None:
    fake_ud(env)
    keeper_auth["typed"] = typed_data(primaryType="Permit")
    result = login(env, keeper_auth, "--wallet", "dev")
    assert result.returncode != 0
    assert "not a Sai trading session request" in result.stderr
    assert "wallet run" not in env.log.read_text()
    assert keeper_auth["signatures"] == []


def ud_show(address: object, state: object = "matching") -> str:
    return json.dumps({"name": "a", "address": address, "cast": {"state": state}})


@pytest.mark.parametrize(
    "output, expected",
    [
        (ud_show(ADDRESS), keeper.UdWallet(ADDRESS, "matching")),
        (ud_show(ADDRESS, "name-conflict"), keeper.UdWallet(ADDRESS, "name-conflict")),
        (json.dumps({"address": ADDRESS}), keeper.UdWallet(ADDRESS, None)),
        (ud_show(ADDRESS, 3), keeper.UdWallet(ADDRESS, None)),
        (
            "note {not json}\n" + json.dumps({"x": {"address": 1}, "address": OTHER}),
            keeper.UdWallet(OTHER, None),
        ),
        (
            'log line\n{"name": "a"}\n' + ud_show(ADDRESS),
            keeper.UdWallet(ADDRESS, "matching"),
        ),
        (ud_show("0x123"), None),
        (ud_show("0x" + "g" * 40), None),
        ("", None),
    ],
)
def test_parse_wallet(output: str, expected: keeper.UdWallet | None) -> None:
    assert keeper.parse_wallet(output) == expected
