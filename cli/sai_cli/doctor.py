"""`sai doctor`: check the setup and say what is broken and how to fix it.

Doctor is read-only. It never creates `$SAI_HOME/trader`, never changes a
file mode, and opens the intent ledger read-only, because `storage.db`
creates and migrates it. Each check returns pass, warn, fail, or skipped
with a fix. Every string leaves through `tools.redact` and every URL
through `info.public_url`, so the session token is never printed.

To add a check, write a function that takes a `Context` and returns a
`Check` or a list of them (a check that repeats, such as one per MCP client),
then list it in LOCAL_CHECKS or NETWORK_CHECKS. Network checks run in
parallel and `--offline` skips them.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

from .core import keeper, margin, storage, stream
from .mcp_server import info, tools

SCHEMA_VERSION = 1
NETWORK_TIMEOUT = 5.0
EXPIRY_WARNING = timedelta(hours=24)
STATUSES = ("pass", "warn", "fail", "skipped")
SIGN_IN = "sai auth login --account NAME, or sai auth import"
REINSTALL = "uv tool install --reinstall git+https://github.com/SaiDotFun/open-sai"


@dataclass
class Check:
    id: str
    status: str
    summary: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix,
            "details": self.details,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True)
class Context:
    """What the checks read: endpoints, the session state, and the clock."""

    config: tools.Config
    session: dict[str, Any]
    now: datetime
    path_env: str = ""


CheckFn = Callable[[Context], "Check | Sequence[Check]"]


def short(address: str) -> str:
    return f"{address[:6]}…" if len(address) > 6 else address


def endpoint_flag(config: tools.Config) -> str:
    """The `--endpoint` flag a fix command needs, or "" for mainnet."""
    if config.endpoint == keeper.MAINNET_HTTP:
        return ""
    return f"--endpoint {info.public_url(config.endpoint)} "


def loose_modes(paths: Sequence[Path]) -> list[dict[str, str]]:
    """Return PATHS that group or others can access, with their modes."""
    if os.name == "nt":
        return []
    loose: list[dict[str, str]] = []
    for path in paths:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if mode & 0o077:
            loose.append({"path": str(path), "mode": f"{mode:o}"})
    return loose


def with_modes(check: Check, paths: Sequence[Path]) -> Check:
    """Add a warning for any of PATHS that other users can read."""
    loose = loose_modes(paths)
    if not loose:
        return check
    fix = "; ".join(
        f"chmod {'700' if Path(item['path']).is_dir() else '600'} {item['path']}"
        for item in loose
    )
    check.details["looseModes"] = loose
    if check.status == "pass":
        check.status = "warn"
        check.summary += "; files are readable by other users"
        check.fix = fix
    else:
        check.fix = f"{check.fix}; then {fix}" if check.fix else fix
    return check


# Local checks


def sai_on_path(path_env: str) -> list[str]:
    """Return each distinct `sai` executable on PATH_ENV, resolved."""
    names = ("sai.exe", "sai") if os.name == "nt" else ("sai",)
    found: list[str] = []
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = str(candidate.resolve())
                if resolved not in found:
                    found.append(resolved)
    return found


def check_install(ctx: Context) -> Check:
    try:
        version = metadata.version(info.PACKAGE)
    except metadata.PackageNotFoundError:
        return Check(
            "install",
            "fail",
            "Package metadata for sai-cli is missing",
            f"Reinstall: {REINSTALL}",
        )
    provenance = info.provenance()
    installs = sai_on_path(ctx.path_env)
    details = {"version": version, "provenance": provenance, "onPath": installs}
    if len(installs) > 1:
        return Check(
            "install",
            "warn",
            f"{len(installs)} sai installs on PATH; {installs[0]} runs",
            "Keep one; `which -a sai` lists them",
            details,
        )
    if provenance["kind"] == "source-checkout":
        return Check(
            "install",
            "warn",
            f"sai {version} runs from a source checkout at {provenance['path']}",
            f"For a regular install: {REINSTALL}",
            details,
        )
    return Check("install", "pass", f"sai {version} installed", None, details)


def check_session(ctx: Context) -> Check:
    root = storage.home()
    return with_modes(session_status(ctx), [root, root / "session.json"])


def session_status(ctx: Context) -> Check:
    state = ctx.session
    if state.get("error"):
        return Check(
            "session",
            "fail",
            str(state["error"]),
            SIGN_IN,
            {"signedIn": False},
        )
    if not state.get("signedIn"):
        return Check(
            "session",
            "warn",
            "Signed out; MCP trading tools are unavailable",
            SIGN_IN,
            {"signedIn": False},
        )
    address = state["address"]
    details = {
        "address": address,
        "expiresAt": state["expiresAt"],
        "endpoint": state["endpoint"],
        "endpointMatches": state["endpointMatches"],
    }
    if state["expired"]:
        return Check(
            "session",
            "fail",
            f"Session for {short(address)} expired at {state['expiresAt']}",
            SIGN_IN,
            details,
        )
    if not state["endpointMatches"]:
        return Check(
            "session",
            "fail",
            f"Session for {short(address)} belongs to {state['endpoint']}, not"
            f" {info.public_url(ctx.config.endpoint)}",
            f"sai {endpoint_flag(ctx.config)}auth login --account NAME, or run"
            f" sai with --endpoint {state['endpoint']}",
            details,
        )
    left = datetime.fromisoformat(state["expiresAt"]) - ctx.now
    if left < EXPIRY_WARNING:
        minutes = int(left.total_seconds() // 60)
        when = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"
        return Check(
            "session",
            "warn",
            f"Session for {short(address)} expires in {when}",
            SIGN_IN,
            details,
        )
    return Check(
        "session",
        "pass",
        f"Signed in as {short(address)} until {state['expiresAt']}",
        None,
        details,
    )


def open_ledger(path: Path) -> sqlite3.Connection:
    """Open the ledger read-only; SQLite never creates or migrates it."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def check_intents(ctx: Context) -> Check:
    path = storage.home() / "intents.sqlite3"
    if not path.is_file():
        return Check("intents", "pass", "No intent ledger yet", None, {})
    fix_corrupt = (
        f"Copy {path} aside before any trade and restore it from a backup; it"
        " records intents that may still execute"
    )
    try:
        with open_ledger(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > storage.SCHEMA_VERSION:
                return Check(
                    "intents",
                    "fail",
                    f"Intent ledger has schema {version}; this sai reads up to"
                    f" {storage.SCHEMA_VERSION}",
                    "Upgrade sai: uv tool upgrade sai-cli",
                    {"userVersion": version},
                )
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                return Check(
                    "intents",
                    "fail",
                    "Intent ledger failed its integrity check",
                    fix_corrupt,
                    {"userVersion": version},
                )
            tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'"
            ).fetchone()
            rows = (
                connection.execute(
                    "SELECT id, endpoint, account FROM intents"
                    " WHERE status='unknown' ORDER BY rowid DESC"
                ).fetchall()
                if tables
                else []
            )
    except sqlite3.Error:
        return Check("intents", "fail", "Intent ledger is unreadable", fix_corrupt, {})
    state = ctx.session
    account = (
        state["address"].lower()
        if state.get("signedIn") and state.get("endpointMatches")
        else None
    )
    blocking: list[str] = []
    others: list[tuple[str, str]] = []
    for key, endpoint, owner in rows:
        if account is not None and (endpoint, owner) == (
            ctx.config.endpoint,
            account,
        ):
            blocking.append(key)
        else:
            others.append((endpoint, owner))
    accounts = len(set(others))
    details: dict[str, Any] = {
        "userVersion": version,
        "unknown": len(rows),
        "blocking": blocking,
        "otherAccounts": {"unknown": len(others), "accounts": accounts},
    }
    return with_modes(
        intents_status(ctx, blocking, len(others), accounts, details), [path]
    )


def intents_status(
    ctx: Context,
    blocking: list[str],
    others: int,
    accounts: int,
    details: dict[str, Any],
) -> Check:
    if blocking:
        address = ctx.session["address"]
        noun = "intent blocks" if len(blocking) == 1 else "intents block"
        return Check(
            "intents",
            "fail",
            f"{len(blocking)} unknown {noun} trading for {short(address)}",
            f"sai {endpoint_flag(ctx.config)}intent status {address}"
            f" --key {blocking[0]}",
            details,
        )
    if others:
        return Check(
            "intents",
            "warn",
            f"{others} unknown intent{'s' if others != 1 else ''} for"
            f" {accounts} account{'s' if accounts != 1 else ''} not signed in"
            " here; they do not block this session",
            "Sign in as each account and run `sai intent status ADDRESS`",
            details,
        )
    return Check("intents", "pass", "Intent ledger is readable", None, details)


# Network checks


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def check_keeper(ctx: Context) -> Check:
    url = info.public_url(ctx.config.endpoint)
    start = time.monotonic()
    try:
        route = keeper.request(
            ctx.config.endpoint, keeper.ROUTE, timeout=NETWORK_TIMEOUT
        )["oneClickTrading"]
        enabled = bool(route["enabled"])
        chain = route.get("chainId")
    except (keeper.KeeperError, KeyError, TypeError) as exc:
        return Check(
            "keeper",
            "fail",
            f"{url} did not answer: {exc}",
            "Check the network and --endpoint",
        )
    details = {"endpoint": url, "tradingEnabled": enabled, "chainId": chain}
    took = elapsed_ms(start)
    if not enabled:
        return Check(
            "keeper",
            "warn",
            f"{url} answered in {took} ms, but trading is disabled",
            "Wait for Keeper to enable trading; reads still work",
            details,
        )
    return Check("keeper", "pass", f"{url} answered in {took} ms", None, details)


def check_websocket(ctx: Context) -> Check:
    try:
        ws = ctx.config.ws_endpoint
    except stream.StreamError as exc:
        return Check("websocket", "fail", str(exc), "Pass --ws-endpoint")
    url = info.public_url(ws)
    start = time.monotonic()
    try:
        stream.handshake(ws, NETWORK_TIMEOUT)
    except (stream.StreamError, *stream.DISCONNECTS) as exc:
        reason = str(exc) or type(exc).__name__
        return Check(
            "websocket",
            "fail",
            f"{url} sent no connection_ack: {reason}",
            "Check the network and --ws-endpoint",
            {"endpoint": url},
        )
    return Check(
        "websocket",
        "pass",
        f"{url} acknowledged in {elapsed_ms(start)} ms",
        None,
        {"endpoint": url},
    )


def check_lcd(ctx: Context) -> Check:
    url = info.public_url(ctx.config.lcd_endpoint)
    start = time.monotonic()
    try:
        node = margin._get_json(
            f"{ctx.config.lcd_endpoint.rstrip('/')}"
            "/cosmos/base/tendermint/v1beta1/node_info",
            timeout=NETWORK_TIMEOUT,
        )
    except ValueError as exc:
        return Check(
            "lcd",
            "fail",
            f"{url} did not answer: {exc}",
            "Check the network and --lcd-endpoint",
            {"endpoint": url},
        )
    info_block = node.get("default_node_info")
    network = info_block.get("network") if isinstance(info_block, dict) else None
    return Check(
        "lcd",
        "pass",
        f"{url} answered in {elapsed_ms(start)} ms (network {network})",
        None,
        {"endpoint": url, "network": network},
    )


def cross_check_network(checks: list[Check]) -> None:
    """Warn when the LCD serves another chain than Keeper's trading route.

    Margin changes and partial closes read the LCD and refuse a mismatch.
    """
    by_id = {check.id: check for check in checks}
    keeper_check, lcd_check = by_id.get("keeper"), by_id.get("lcd")
    if keeper_check is None or lcd_check is None or lcd_check.status != "pass":
        return
    chain = keeper_check.details.get("chainId")
    network = lcd_check.details.get("network")
    if chain and network and chain != network:
        lcd_check.status = "warn"
        lcd_check.summary += f"; Keeper trades on {chain}"
        lcd_check.fix = "Pass the --lcd-endpoint for Keeper's chain"


LOCAL_CHECKS: list[tuple[str, CheckFn]] = [
    ("install", check_install),
    ("session", check_session),
    ("intents", check_intents),
]
NETWORK_CHECKS: list[tuple[str, CheckFn]] = [
    ("keeper", check_keeper),
    ("websocket", check_websocket),
    ("lcd", check_lcd),
]


def timed(name: str, fn: CheckFn, ctx: Context) -> list[Check]:
    """Run FN, time it, and turn a crash into a failed check."""
    start = time.monotonic()
    try:
        result = fn(ctx)
        checks = [result] if isinstance(result, Check) else list(result)
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide others
        checks = [
            Check(
                name,
                "fail",
                f"Check crashed with {type(exc).__name__}",
                "Report this at https://github.com/SaiDotFun/open-sai/issues",
            )
        ]
    took = elapsed_ms(start)
    for check in checks:
        check.duration_ms = took
    return checks


def run_checks(ctx: Context, offline: bool = False) -> list[Check]:
    """Run local checks in order, then network checks in parallel."""
    checks: list[Check] = []
    for name, fn in LOCAL_CHECKS:
        checks.extend(timed(name, fn, ctx))
    if offline:
        checks.extend(
            Check(name, "skipped", "Skipped with --offline")
            for name, _fn in NETWORK_CHECKS
        )
        return checks
    with concurrent.futures.ThreadPoolExecutor(len(NETWORK_CHECKS)) as pool:
        futures = [pool.submit(timed, name, fn, ctx) for name, fn in NETWORK_CHECKS]
        network = [check for future in futures for check in future.result()]
    cross_check_network(network)
    checks.extend(network)
    return checks


def overall(checks: Sequence[Check]) -> str:
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return "fail"
    return "warn" if "warn" in statuses else "pass"


def context(config: tools.Config, path_env: str | None = None) -> Context:
    return Context(
        config=config,
        session=tools.session_state(config),
        now=datetime.now(UTC),
        path_env=os.environ.get("PATH", "") if path_env is None else path_env,
    )


def report(checks: Sequence[Check], now: datetime | None = None) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": (now or datetime.now(UTC)).isoformat(),
        "sai": {"version": info.version(), "install": info.provenance()},
        "overall": overall(checks),
        "checks": [check.to_json() for check in checks],
    }


def render_json(value: dict[str, Any]) -> str:
    return tools.redact(json.dumps(value, separators=(",", ":"), default=str))


def render_text(checks: Sequence[Check]) -> str:
    width = max([9, *(len(check.id) for check in checks)]) + 2
    lines: list[str] = []
    for check in checks:
        lines.append(f"{check.status:<8}{check.id:<{width}}{check.summary}")
        if check.fix:
            lines.append(f"{'':<{8 + width}}fix: {check.fix}")
    counts = ", ".join(
        f"{sum(c.status == s for c in checks)} {s}"
        for s in STATUSES
        if any(c.status == s for c in checks)
    )
    lines.append(f"\noverall: {overall(checks)} ({counts})")
    return tools.redact("\n".join(lines))
