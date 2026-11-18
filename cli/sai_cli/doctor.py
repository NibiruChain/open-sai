"""`sai doctor`: check the setup, fix what is safe to fix, and say how to
fix the rest.

Each check returns pass, warn, fail, skipped, or fixed. A fixed check lists
what it changed in `changes`. Doctor fixes only what it can from inspection:
it creates `$SAI_HOME/trader`, tightens file modes, migrates the intent
ledger, and reconciles unknown intents for the signed-in account with Keeper
(a read; no trade is placed or cancelled). It never deletes or rewrites the
session. `--no-fix` reports without changing anything.

Every string leaves through `tools.redact` and every URL through
`info.public_url`, so the session token is never printed.

To add a check, write a function that takes a `Context` and returns a
`Check` or a list of them (a check that repeats, such as one per MCP client),
then list it in LOCAL_CHECKS or NETWORK_CHECKS. Network checks run in
parallel and `--offline` skips them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

from .core import keeper, margin, reconcile, storage, stream
from .mcp_server import info, tools

SCHEMA_VERSION = 1
NETWORK_TIMEOUT = 5.0
# Wall-clock limit for all network checks together, and separately for
# reconciling unknown intents.
NETWORK_DEADLINE = NETWORK_TIMEOUT + 2
EXPIRY_WARNING = timedelta(hours=24)
STATUSES = ("pass", "fixed", "warn", "fail", "skipped")
SIGN_IN = "sai auth login --account NAME, or sai auth import"
REINSTALL = "uv tool install --reinstall git+https://github.com/SaiDotFun/open-sai"
DIR_MODE, FILE_MODE = 0o700, 0o600


@dataclass
class Check:
    id: str
    status: str
    summary: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    changes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix,
            "changes": self.changes,
            "details": self.details,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True)
class Context:
    """What the checks read: endpoints, the session state, the clock, and
    whether they may fix things or reach the network."""

    config: tools.Config
    session: dict[str, Any]
    now: datetime
    path_env: str = ""
    fix: bool = True
    offline: bool = False


CheckFn = Callable[[Context], "Check | Sequence[Check]"]


def short(address: str) -> str:
    return f"{address[:6]}…" if len(address) > 6 else address


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def endpoint_flag(config: tools.Config) -> str:
    """The `--endpoint` flag a fix command needs, or "" for mainnet."""
    if config.endpoint == keeper.MAINNET_HTTP:
        return ""
    return f"--endpoint {info.public_url(config.endpoint)} "


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


def loose_modes(paths: Sequence[Path]) -> list[tuple[Path, int, int]]:
    """Return each of PATHS that group or others can access, with its mode
    and the private mode it should have."""
    if os.name == "nt":
        return []
    loose: list[tuple[Path, int, int]] = []
    for path in paths:
        try:
            info_ = path.stat()
        except OSError:
            continue
        mode = stat.S_IMODE(info_.st_mode)
        if mode & 0o077:
            wanted = DIR_MODE if stat.S_ISDIR(info_.st_mode) else FILE_MODE
            loose.append((path, mode, wanted))
    return loose


def check_home(ctx: Context) -> Check:
    """`$SAI_HOME/trader` exists, and only its owner can read it or its files."""
    root = storage.home()
    if not root.is_dir():
        if not ctx.fix:
            return Check(
                "home",
                "pass",
                f"{root} does not exist yet; sai creates it on first use",
                None,
                {"path": str(root)},
            )
        storage.directory()
        return Check(
            "home",
            "fixed",
            f"Created {root}",
            None,
            {"path": str(root)},
            changes=[f"created {root} with mode {DIR_MODE:o}"],
        )
    paths = [root, *sorted(path for path in root.iterdir() if not path.is_symlink())]
    loose = loose_modes(paths)
    details: dict[str, Any] = {
        "path": str(root),
        "looseModes": [
            {"path": str(path), "mode": f"{mode:o}"} for path, mode, _ in loose
        ],
    }
    if not loose:
        return Check("home", "pass", f"{root} is private", None, details)
    commands = "; ".join(f"chmod {wanted:o} {path}" for path, _, wanted in loose)
    if not ctx.fix:
        return Check(
            "home",
            "warn",
            f"{plural(len(loose), 'path')} under {root} readable by other users",
            commands,
            details,
        )
    changes: list[str] = []
    failed: list[str] = []
    for path, mode, wanted in loose:
        try:
            path.chmod(wanted)
            changes.append(f"chmod {path} {mode:o} -> {wanted:o}")
        except OSError:
            failed.append(f"chmod {wanted:o} {path}")
    if failed:
        return Check(
            "home",
            "fail",
            f"Could not make {plural(len(failed), 'path')} private",
            "; ".join(failed),
            details,
            changes=changes,
        )
    return Check(
        "home",
        "fixed",
        f"Made {plural(len(changes), 'path')} under {root} private",
        None,
        details,
        changes=changes,
    )


def check_session(ctx: Context) -> Check:
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


def ledger_path() -> Path:
    return storage.home() / "intents.sqlite3"


def unknown_rows(path: Path) -> list[tuple[str, str, str]]:
    """Return (key, endpoint, account) for each unknown intent at PATH."""
    with sqlite3.connect(path, timeout=5) as connection:
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'"
        ).fetchone()
        if not tables:
            return []
        return connection.execute(
            "SELECT id, endpoint, account FROM intents"
            " WHERE status='unknown' ORDER BY rowid DESC"
        ).fetchall()


def reconcile_keys(
    endpoint: str, address: str, keys: Sequence[str], deadline: float
) -> dict[str, Any]:
    """Run `sai intent status` reconciliation for KEYS within DEADLINE
    seconds. Returns any error; a daemon thread left running at the deadline
    cannot keep the process alive."""
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            for key in keys:
                reconcile.reconcile(endpoint, address, key)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        outcome["error"] = f"no answer within {deadline:g} s"
    return outcome


def check_intents(ctx: Context) -> Check:
    path = ledger_path()
    if not path.is_file():
        return Check("intents", "pass", "No intent ledger yet", None, {})
    fix_corrupt = (
        f"Copy {path} aside before any trade and restore it from a backup; it"
        " records intents that may still execute"
    )
    changes: list[str] = []
    try:
        with sqlite3.connect(path, timeout=5) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        connection.close()
        if version > storage.SCHEMA_VERSION:
            return Check(
                "intents",
                "fail",
                f"Intent ledger has schema {version}; this sai reads up to"
                f" {storage.SCHEMA_VERSION}",
                "Upgrade sai: uv tool upgrade sai-cli",
                {"userVersion": version},
            )
        if integrity != "ok":
            return Check(
                "intents",
                "fail",
                "Intent ledger failed its integrity check",
                fix_corrupt,
                {"userVersion": version},
            )
        if version < storage.SCHEMA_VERSION and ctx.fix:
            storage.db().close()
            changes.append(
                f"migrated the ledger from schema {version} to {storage.SCHEMA_VERSION}"
            )
            version = storage.SCHEMA_VERSION
        rows = unknown_rows(path)
    except (sqlite3.Error, OSError):
        return Check("intents", "fail", "Intent ledger is unreadable", fix_corrupt, {})
    state = ctx.session
    address = state.get("address") if state.get("endpointMatches") else None
    blocking = [
        key
        for key, endpoint, owner in rows
        if address is not None
        and (endpoint, owner) == (ctx.config.endpoint, address.lower())
    ]
    details: dict[str, Any] = {"userVersion": version}
    if blocking and ctx.fix and not ctx.offline and address is not None:
        outcome = reconcile_keys(
            ctx.config.endpoint, address, blocking, NETWORK_DEADLINE
        )
        if "error" in outcome:
            details["reconcileError"] = outcome["error"]
        rows = unknown_rows(path)
        remaining = {key for key, _endpoint, _owner in rows}
        for key in blocking:
            if key not in remaining:
                changes.append(f"reconciled intent {key} with Keeper")
        blocking = [key for key in blocking if key in remaining]
    others = [(endpoint, owner) for key, endpoint, owner in rows if key not in blocking]
    accounts = len(set(others))
    details.update(
        unknown=len(rows),
        blocking=blocking,
        otherAccounts={"unknown": len(others), "accounts": accounts},
    )
    check = intents_status(ctx, blocking, len(others), accounts, details)
    check.changes = changes
    if changes and check.status == "pass":
        check.status = "fixed"
        summary = "; ".join(changes)
        check.summary = summary[0].upper() + summary[1:]
    return check


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
            f"{plural(others, 'unknown intent')} for"
            f" {plural(accounts, 'account')} not signed in"
            " here; they do not block this session",
            "Sign in as each account and run `sai intent status ADDRESS`",
            details,
        )
    if details["userVersion"] < storage.SCHEMA_VERSION:
        return Check(
            "intents",
            "warn",
            f"Intent ledger has schema {details['userVersion']}; the next trade"
            " migrates it",
            "sai doctor",
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
    if not isinstance(network, str) or not network:
        return Check(
            "lcd",
            "fail",
            f"{url} answered without a chain network in node_info",
            "Check --lcd-endpoint points at a Nibiru REST (LCD) service",
            {"endpoint": url},
        )
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
    ("home", check_home),
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


def run_network(ctx: Context, deadline: float) -> list[Check]:
    """Run NETWORK_CHECKS in parallel and stop waiting after DEADLINE seconds.

    httpx timeouts apply per read, so a server that drips bytes could hold a
    check open indefinitely. Each check runs in a daemon thread; one still
    running at the deadline becomes a fail, and its thread cannot keep the
    process alive.
    """
    results: dict[str, list[Check]] = {}

    def work(name: str, fn: CheckFn) -> None:
        results[name] = timed(name, fn, ctx)

    threads = [
        threading.Thread(target=work, args=(name, fn), daemon=True)
        for name, fn in NETWORK_CHECKS
    ]
    end = time.monotonic() + deadline
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(end - time.monotonic(), 0))
    checks: list[Check] = []
    for name, _fn in NETWORK_CHECKS:
        if name in results:
            checks.extend(results[name])
        else:
            checks.append(
                Check(
                    name,
                    "fail",
                    f"No answer within {deadline:g} s",
                    "Check the network and the endpoint options",
                    duration_ms=round(deadline * 1000),
                )
            )
    return checks


def run_checks(ctx: Context) -> list[Check]:
    """Run local checks in order, then network checks in parallel."""
    checks: list[Check] = []
    for name, fn in LOCAL_CHECKS:
        checks.extend(timed(name, fn, ctx))
    if ctx.offline:
        checks.extend(
            Check(name, "skipped", "Skipped with --offline")
            for name, _fn in NETWORK_CHECKS
        )
        return checks
    network = run_network(ctx, NETWORK_DEADLINE)
    cross_check_network(network)
    checks.extend(network)
    return checks


def overall(checks: Sequence[Check]) -> str:
    """fail if any check failed, warn if any warned, else pass; fixed and
    skipped count as pass."""
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return "fail"
    return "warn" if "warn" in statuses else "pass"


def session_state(config: tools.Config) -> dict[str, Any]:
    """Return `tools.session_state`, or an error state if the stored session
    cannot be interpreted, such as an expiry without a time zone."""
    try:
        state = tools.session_state(config)
        if state.get("signedIn"):
            expiry = datetime.fromisoformat(state["expiresAt"])
            if expiry.tzinfo is None:
                raise TypeError("expiry has no time zone")
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return {
            "signedIn": False,
            "error": f"Stored session is invalid ({type(exc).__name__});"
            f" {storage.SIGN_IN_AGAIN}",
        }
    return state


def context(
    config: tools.Config,
    path_env: str | None = None,
    *,
    fix: bool = True,
    offline: bool = False,
) -> Context:
    return Context(
        fix=fix,
        offline=offline,
        config=config,
        session=session_state(config),
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
        lines.extend(f"{'':<{8 + width}}did: {c}" for c in check.changes)
        if check.fix:
            lines.append(f"{'':<{8 + width}}fix: {check.fix}")
    counts = ", ".join(
        f"{sum(c.status == s for c in checks)} {s}"
        for s in STATUSES
        if any(c.status == s for c in checks)
    )
    lines.append(f"\noverall: {overall(checks)} ({counts})")
    return tools.redact("\n".join(lines))
