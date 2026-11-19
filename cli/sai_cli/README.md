# Sai CLI

Install `sai` as the [root README](../../README.md) shows. From a checkout, run
`uv sync`, then `uv run sai --help`.
The CLI connects to mainnet Keeper by default.

## Settings

`$SAI_HOME/config.toml` (default `~/.sai/config.toml`) holds settings for both
the CLI and `sai mcp serve`. Each setting comes from the first of these that sets
it: a flag, an environment variable, the file, the mainnet default.

| Key                  | Flag             | Variable            | Default                 |
| -------------------- | ---------------- | ------------------- | ----------------------- |
| `keeper.endpoint`    | `--endpoint`     | `SAI_ENDPOINT`      | mainnet Keeper GraphQL  |
| `keeper.ws_endpoint` | `--ws-endpoint`  | `SAI_WS_ENDPOINT`   | derived from the above  |
| `lcd.endpoint`       | `--lcd-endpoint` | `SAI_LCD_ENDPOINT`  | `https://lcd.nibiru.fi` |
| `update.auto`        |                  | `SAI_UPDATE_AUTO`   | `true`                  |
| `logs.enabled`       |                  | `SAI_LOG`           | `true`                  |
| `logs.keep_days`     |                  | `SAI_LOG_KEEP_DAYS` | `14`                    |

```bash
sai config list                      # every key, its value, and its source
sai config get keeper.endpoint       # the exact value
sai config set keeper.endpoint URL
sai config unset keeper.endpoint
```

`config list --json` returns `{"path", "settings": {KEY: {"value",
"source", "env", "default"}}}`, where `source` is `flag`, `env`, `file`,
`default`, or `derived`. `list` drops credentials from URLs; `get` prints the
exact value. An unknown key or invalid value exits 2 and lists the valid keys.

The first `set`, or an `unset` that removes a value, writes the whole file
with every key commented out and explained, at mode 0600 in a 0700
directory. Unsetting a key that is already unset writes nothing. Later edits keep your
comments, and a lock file makes concurrent edits run one at a time. The
CLI reads a section written as an inline table, dotted keys, or only
subtables, but `set` and `unset` refuse to edit it (exit 2) until you rewrite
it as a `[section]` table. Unsetting a key that is already unset leaves the
file alone. Keys the CLI does not know produce a warning and are
ignored; a known section that is not a table is an error.

A `keeper.ws_endpoint` from a lower layer than `keeper.endpoint` is ignored,
and the CLI derives the WebSocket from the Keeper endpoint instead. So
`sai --endpoint URL` never pairs `URL` with the WebSocket of the Keeper in the
file. The per-command `--ws-endpoint` and `--lcd-endpoint` options still
work and override the setting for that command.

## Connecting

The CLI trades with a Keeper session token, the same HS256 JWT Sai Web keeps
in the browser. Keeper issues it for 30 days in production. It authorizes
every trade kind on the account's deposited funds, and Keeper cannot revoke it
yet: signing out of Sai Web clears only the browser copy, not the CLI's.

`auth import` stores a token copied from Sai Web. The **Connect Sai CLI /
MCP** button that copies it is in review
(https://github.com/SaiDotFun/sai-web/pull/1591); until it ships, use
`auth login --account` below.

- With a terminal, it asks for the token at a hidden prompt. Press Enter
  without typing to read the clipboard.
- Without a terminal, as when an agent runs it, it reads the clipboard with the
  first command that works: `pbpaste`, `wl-paste`, `xclip`, or `powershell.exe
  Get-Clipboard` (WSL and Windows). If none works, as over SSH, it exits
  non-zero and asks for the person to run `sai auth import` in their own
  terminal.
- It never reads the token from arguments, environment variables, or piped
  stdin, and never prints it. Like `auth login` and `auth status`, it prints
  only `address`, `expiresAt`, and `endpoint`.
- After storing the token, it reads the clipboard again and clears it only if
  it still holds the token. Anything copied in the meantime stays.

`auth login` signs Keeper's challenge with a local key. Before anything
signs, the CLI checks that the typed data has primary type
`UserTradingSession`, domain `Sai` version `1`, your address, and the issued
challenge nonce.

- `auth login --account NAME` uses the Foundry keystore `NAME`. It runs `cast
  wallet address --account NAME` and `cast wallet sign --account NAME --data
  --from-file PATH` on your terminal, and `cast` prompts for the keystore
  password.
- `auth login --wallet NAME` is for people who use the `ud` tool. The CLI
  reads the address from `ud evm wallet show NAME` and signs with `ud evm
  wallet run NAME -- wallet sign --data --from-file PATH`, which needs no
  prompt. Login fails unless ud reports the wallet's Cast keystore as
  `matching`.

`--address ADDRESS` is optional. With `--account`, it replaces `cast wallet
address` and saves one password prompt. With `--wallet`, the CLI refuses to
sign when it differs from the wallet's address.

### Several Accounts

Each `auth login` or `auth import` connects an account and makes it active.
Signing in with another account keeps the ones already connected; signing in
again with the same address refreshes its session.

- `auth list` shows each connected account, its expiry, and which is active.
- `auth use ADDRESS` makes another connected account active.
- `--as ADDRESS`, before the command, runs one command as another connected
  account: `sai --as 0x... trade close 3`.
- `auth logout [ADDRESS]` removes one account, by default the active one.
  The others stay connected. After removing the active account, none is
  active until `auth use`.

`auth logout` makes no network call, and since Keeper cannot revoke tokens
yet, a copy of the token elsewhere, such as in Sai Web, stays valid until it
expires.

Each session is bound to its account and Keeper endpoint and expires with
the token. The CLI writes `$SAI_HOME/trader/sessions.json` with mode 0600,
encrypted by AES-GCM with a fixed key built into the CLI, like the Cosmos SDK
`test` keyring. Nothing prompts, so agents can run authenticated commands.
This only keeps the token out of plaintext; anyone who can read the file and
this source can decrypt it. Older versions could encrypt the session with a
passphrase. The CLI refuses those files and asks you to run `sai auth import`
or `sai auth login` again. A single-account `session.json` from before
multiple accounts still loads and moves into `sessions.json` at the next
sign-in or switch.


## MCP Server

`sai mcp serve` runs the stdio MCP server for agent clients. Register it with
`sai mcp install claude`, `codex`, or `cursor`, or `--all` for every detected
client; `sai mcp list` and `sai mcp uninstall` show and remove registrations.
A bare `sai mcp` prints help. The
server writes only MCP messages to stdout and logs to stderr; run on a
terminal, it prints one stderr line saying an agent client should launch it.
`--endpoint`, `--ws-endpoint`, and `--lcd-endpoint` pick the network as the
top layer of [Settings](#settings), falling back to the same `sai` global
flags, then `SAI_ENDPOINT`, `SAI_WS_ENDPOINT`, and `SAI_LCD_ENDPOINT`, then
the config file, then mainnet. They resolve once, at start.

`install` registers the unresolved `sai` on `PATH` (normally the
`~/.local/bin/sai` link that survives updates) as `<sai> mcp serve`, with no
environment except `SAI_HOME` when it is set. It refuses a `sai` inside the uv
cache with code `UNSTABLE_PATH`, and warns when the `sai` on `PATH` is not the
install that is running. A server command after `--` replaces the default, for
a dev checkout. Claude Code and Codex change through `claude mcp add|remove`
and `codex mcp add|remove`; Cursor through an atomic edit of
`~/.cursor/mcp.json` that keeps other servers and the file mode. Reads take
only the `sai` entry's command, args, `SAI_HOME`, and the names of any other
environment settings, and never run `claude mcp get`, which starts the
server. The only environment value output shows is `SAI_HOME`, a path; other
settings appear by name in `staleEnv`, and an entry with any of them, or a
disabled Codex entry, is not current and gets replaced. `list` shows every
scope's entry under `registrations` and marks the one the client uses as
`effective` (Claude Code: local, then project, then user; Cursor: project,
then user). `uninstall` removes every scope's entry unless `--scope` names
one. Output shows a registered command in full only when it is the one
`install` would register; any other entry shows only its program name and
`args: "<hidden>"`, since its args may hold a token or credential URL. A
trusted Codex project's `.codex/config.toml` can define its own `sai` entry
that overrides the user's. Codex decides which project configs load, so sai
runs `codex mcp get sai --json` twice, from `$CODEX_HOME` for the user entry
and from the working directory for the entry Codex uses there. When they
differ (compared by a hash of everything Codex reports for the server, env values,
URL, and tool filters included), `list`
reports a `project` entry as effective, and `install` and `uninstall` fail
for Codex rather than edit project files. After each change sai reads the
client again: the entry must match, or be gone, and for Codex the answer from
the working directory must agree too. Otherwise the client fails with "a
project .codex/config.toml entry is still active", which catches a project
entry identical to the user one. An entry with a Codex tool filter
(`enabled_tools` or `disabled_tools`) lists it under `extraSettings` and is not
current. `reconnectNeeded` is true whenever a client config was written, even
if the check afterwards failed. The Cursor edit re-reads the file just before the rename
and merges again once if something else changed it. A plain file has no
compare-and-swap, so a write in the instant between that re-read and the
rename can still be lost. Results report `added`, `replaced`, `unchanged`, `removed`,
`absent`, `failed`, or `planned` (with `--dry-run`) per client, and the
command exits 1 if any client failed. Without a terminal, `install` with no
client lists clients and changes nothing, and `uninstall` with no client is a
usage error.

The server lists every tool regardless of sign-in. Each `trade_*` call checks
the session of the account it acts as (the active one, or its `address`)
before it rebuilds the preview: no session returns
`SIGNED_OUT`, an expired one `SESSION_EXPIRED`, and one for another endpoint
or an unreadable file `SESSION_UNAVAILABLE`. Each message tells the agent to
have the user run `sai auth login` or `sai auth import`. A signed-in trade
still needs the `previewDigest` of the preview the user confirmed.

## Commands

Commands include `market list`, `market prices`, `account positions ADDRESS`,
`account balances ADDRESS`, `account history ADDRESS`, `trade open`, `trade
close`, `trade partial-close`, `trade tpsl`, `intent status ADDRESS --key KEY`,
and live `watch` streams. Run any command with `--help` for details. Trading
requires a funded account; funding is performed separately. Trade commands
support `--dry-run` to inspect the local semantic request.
For a live market order, the CLI refreshes the price before admission and
reports that reference price after acceptance. A dry run marks the price as
requiring refresh and may include an optional supplied reference. A partial close sizes the
reduction from the Perp contract's collateral, refuses when Keeper's index
disagrees with the contract, and rechecks the position before submission. It
accepts `--lcd-endpoint` like `trade margin`. Market schedule and other Keeper admission checks
remain authoritative.

`trade leverage TRADE_ID --leverage VALUE` sets a target leverage.
`trade margin TRADE_ID --direction add|remove --amount USDC` reads the exact
position and exchange balance from the Perp contract, then quotes a target
leverage. The margin amount is a quote, not a cap: Keeper executes a leverage
change, and the actual margin delta may differ if the position changes after
the final check. `--dry-run` reads live state and prints the quote without
submitting an intent. Use `--lcd-endpoint` to select the matching network's
Nibiru REST service; the CLI checks that its chain matches Keeper.
The CLI refuses the quote if the indexed position and contract disagree. Wait
for the index to synchronize and retry; no margin intent has been submitted.

The CLI persists non-secret request identities under `$SAI_HOME/trader` or
`~/.sai/trader`. A network error after submission leaves the outcome unknown,
and an unknown intent blocks new trades on the account. Reconcile it with
`intent status ADDRESS --key KEY`. If Keeper has no record of the key five
minutes after submission, past Keeper's two-minute admission expiry, the CLI
marks it `absent` and trading resumes. A connection that fails before the
request is sent marks the intent `not_sent`, which does not block. Keeper
admission does not establish execution or finality.

`sai doctor` checks the install, `$SAI_HOME/trader`, `$SAI_HOME/logs`, the
session, the intent ledger, and the Keeper HTTP, WebSocket, and LCD
endpoints, and fixes what it safely can. It creates `$SAI_HOME/trader`,
removes group and other access from it and the files sai keeps there (the
connected accounts, the ledger and its journals, and the shell history) and
from the logs directory and its log files, migrates the intent ledger, and reconciles
unknown intents for the signed-in account the way `sai intent status` does
(a Keeper read; it never places or cancels a trade). It never deletes or
rewrites the session, never writes through a symlink in `$SAI_HOME/trader`,
and never prints the session token. Without fixes it reads the ledger from a
temporary copy, so it writes nothing at all.

Each check prints pass, fixed, warn, fail, or skipped, with what changed or
the command that fixes it. Doctor exits 1 if any check still fails;
`sai --json doctor` prints the same report as JSON. `--no-fix` reports
without changing anything, log lines included, and `--offline` skips the network checks, which
otherwise run in parallel and stop after 7 seconds in total. The
endpoints come from [Settings](#settings); `sai doctor --ws-endpoint` and
`--lcd-endpoint` override them for one run.

## Logs

The CLI and the MCP server append what they do to `$SAI_HOME/logs` (default
`~/.sai/logs`), one JSON object per line. Each surface has one file per UTC
day: `cli-YYYY-MM-DD.jsonl`, `mcp-YYYY-MM-DD.jsonl` for `sai mcp serve`, and
`update-YYYY-MM-DD.jsonl` for `sai update`. The directory has mode 0700 and
the files 0600.

```bash
sai logs path                        # the directory
sai logs tail                        # the last 20 events, oldest first
sai logs tail -n 50 --surface mcp    # cli, mcp, or update
sai logs tail --event keeper.request
sai logs tail --event intent         # a prefix matches intent.reserved, intent.status, intent.keeper
sai --json logs tail                 # the stored JSON objects, one per line
```

Plain `tail` output is one line per event: UTC time, surface, level, event,
then `key=value` fields. `sai --json logs path` returns `{"dir", "enabled",
"keepDays", "files"}`.

Every line has these fields:

| Field     | Meaning                                                        |
| --------- | -------------------------------------------------------------- |
| `ts`      | UTC time with milliseconds, such as `2026-11-18T14:03:07.412Z` |
| `level`   | `info`, `warn`, or `error`                                     |
| `event`   | The event name, below                                          |
| `surface` | `cli`, `mcp`, or `update`; also the file the line is in        |
| `pid`     | Process id                                                     |
| `version` | The `sai` version that wrote the line                          |
| `runId`   | One id per process; it ties a command's lines together         |
| `seq`     | The line's number within the run                               |

Lines written while a CLI command runs also carry `command`, such as `market
prices`. Lines written during an MCP tool call carry `tool` and `callId`.

| Event              | When                                    | Fields                                                                                               |
| ------------------ | --------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `command.start`    | A CLI command starts                    | `command`, `argv`                                                                                    |
| `command.end`      | It ends                                 | `exitCode`, `durationMs`, `error`                                                                    |
| `mcp.server_start` | `sai mcp serve` starts                  | `keeper`, `websocket`, `lcd`                                                                         |
| `mcp.tool_call`    | An agent calls a tool                   | `tool`, `callId`, `arguments`, `unknownArguments`                                                    |
| `mcp.tool_result`  | The tool returns                        | `ok`, `code`, `error`, `errorType`, `account`, `status`, `intentKey`, `previewDigest`, `durationMs`  |
| `keeper.request`   | Each Keeper GraphQL request             | `operation`, `endpoint`, `status`, `httpStatus`, `keeperErrors`, `error`, `addr`, `authenticated`, `durationMs` |
| `intent.reserved`  | A trade intent gets its key             | `intentKey`, `kind`, `status`, `addr`, `tradeId`, `endpoint`                                         |
| `intent.status`    | Its local status changes                | `intentKey`, `kind`, `previous`, `status`, `addr`, `endpoint`                                        |
| `intent.keeper`    | Keeper reports its record of the intent | `intentKey`, `kind`, `lifecycleStep`, `execActor`, `errorCode`, `errorMsg`, `txHash`, `found`, `addr` |
| `session.saved`    | Sign-in stores a session                | `addr`, `expiresAt`, `endpoint`, `connectedAccounts`                                                 |
| `session.active`   | `auth use` switches account             | `addr`                                                                                               |
| `session.removed`  | `auth logout` removes one               | `addr`, `connectedAccounts`                                                                          |
| `session.expired`  | A command finds the session expired     | `addr`, `expiresAt`                                                                                  |
| `doctor.result`    | `sai doctor` finishes                   | `overall`, `fix`, `offline`, `checks`                                                                |
| `update.check`     | `sai update --check` finishes           | `ok`, `installed`, `latest`, `updateAvailable`, `installKind`, `error`                               |
| `log.dropped`      | Events were lost because the disk was slow | `count`                                                                                           |

A `keeper.request` has `status` `ok`, `error`, `rejected` (Keeper refused
admission), or `not_sent` (the connection failed first). Its `operation` is
`markets`, `prices`, `positions`, `history`, `intents`, `submit`, and so on.
To follow one trade, filter on its `intentKey`: `intent.reserved` (status
`unknown`), then `keeper.request` with operation `submit`, then
`intent.status` as it becomes `accepted`, `rejected`, `not_sent`, `terminal`,
or `absent`, with `intent.keeper` lines for each time `intent status` asked
Keeper.

The logs never hold the session token, a signature, clipboard text, request
variables, or response data. sai passes none of these to the logger, and the
logger also treats every string as untrusted:

- A URL anywhere, including in `argv` and `endpoint`, is cut to its scheme,
  host, and port, so an API key in a path or query is not written.
- Stored session tokens, anything shaped like a JWT, and hex strings of 100
  digits or more, such as a signature, are replaced.
- Text Keeper or a tool returned (`keeperErrors`, `errorMsg`, `error`) is
  cut to 300 characters and also loses hex strings of 64 digits or more.
- `mcp.tool_call` logs only the parameters the tool defines.
  `unknownArguments` counts the rest, and a call to a tool the server does
  not have is logged as tool `unknown` with no arguments.
- Other strings are cut at 2000 characters.

Logging never slows a command or a trade. Events go on an in-memory queue of
2048, and one background thread redacts and writes them, so a slow disk
cannot delay an MCP reply or the step between reserving an intent and
submitting it. If the queue fills, sai drops events and later writes one
`log.dropped` line with the count. At exit sai waits up to two seconds for
the queue to empty.

sai writes only inside a real `$SAI_HOME/logs` directory that you own. It
does not follow a symlink at that path, and it does not append to a symlink,
a FIFO, a file with other hard links, or a file someone else owns. It
removes group and other access from the directory and from a log file before
appending. A logs directory that cannot be written never fails a command;
the lines are lost. `sai doctor` reports a symlink or a file at the logs
path.

Files older than `logs.keep_days` days, counting today, are deleted once per
UTC day. A long-running MCP server checks again when the day changes. Only
files named `cli-`, `mcp-`, or `update-YYYY-MM-DD.jsonl` are deleted; other
files in the directory are left alone.

`sai config set logs.enabled false` or `SAI_LOG=0` turns logging off;
nothing is then written or created. A logs setting sai cannot read, such as
`SAI_LOG_KEEP_DAYS=two`, or a config file that does not parse, also turns
logging off. `sai logs`, `sai doctor --no-fix`, `--help`, `--version`, and
`--skill` never write log lines.

Each line is appended with a single write, so concurrent `sai` processes and
MCP servers never interleave within a line. `sai logs tail` reads each file
from its end, so it stays fast on a large file.

Run `sai` with no arguments for an interactive shell. `help` or `help COMMAND`
prints usage, Ctrl-C cancels the running command, and Ctrl-D or `exit` leaves.
In a terminal, the shell keeps command history in `$SAI_HOME/trader/history`.

`utils/repl_skin.py` is adapted from CLI-Anything
(https://github.com/HKUDS/CLI-Anything) under the Apache License 2.0. See the
repository license.
