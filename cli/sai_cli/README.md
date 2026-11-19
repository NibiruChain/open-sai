# Sai CLI

Install `sai` as the [root README](../../README.md) shows. From a checkout, run
`uv sync`, then `uv run sai --help`.
The CLI connects to mainnet Keeper by default.

## Settings

`$SAI_HOME/config.toml` (default `~/.sai/config.toml`) holds settings for both
the CLI and `sai mcp serve`. Each setting comes from the first of these that sets
it: a flag, an environment variable, the file, the mainnet default.

| Key                  | Flag             | Variable           | Default                 |
| -------------------- | ---------------- | ------------------ | ----------------------- |
| `keeper.endpoint`    | `--endpoint`     | `SAI_ENDPOINT`     | mainnet Keeper GraphQL  |
| `keeper.ws_endpoint` | `--ws-endpoint`  | `SAI_WS_ENDPOINT`  | derived from the above  |
| `lcd.endpoint`       | `--lcd-endpoint` | `SAI_LCD_ENDPOINT` | `https://lcd.nibiru.fi` |
| `update.auto`        |                  | `SAI_UPDATE_AUTO`  | `true`                  |

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

## Updates

```bash
sai update                # install the latest release
sai update --check        # report the installed and latest versions only
sai update --to 0.2.0     # install that release; the only way to go back
sai update --source       # build the release from Git instead of its wheel
```

`sai update` works in this order, so a release that cannot start never
replaces a working `sai`:

1. Download the release wheel and `SHA256SUMS` and check the hash.
2. Install the wheel into a throwaway environment, run its `sai --version`,
   and import the CLI and the MCP server there.
3. Get a rollback copy of the current version: the wheel kept in
   `$SAI_HOME/cache/wheels` if its hash still matches, or the current release
   downloaded and checked again.
4. Install the wheel with `uv tool install --force`.
5. Run the installed `sai --version` and the imports again. If that fails, or
   on Ctrl-C, put the previous version back from the rollback copy.

It exits 0 when sai is updated or already current and 1 on a failure. uv and
the checks run in a neutral directory with an isolated Python, so nothing is
read from the directory you are in.

Only a `uv tool install` of a release, or of this repository's Git source, is
updated. A checkout, a `uvx` run, a fork, or any other install gets a note and
exit code 3; `sai doctor` warns about those in its `install` check. sai never
moves to an older version by itself. `sai update` also stops with exit code 3,
before changing anything, when uv's current tool or executable directory is
not where this `sai` is installed, or when the `sai` on your `PATH` now
belongs to another install.

The update does not move `~/.local/bin/sai` or change anything else under
`$SAI_HOME`, so MCP registrations keep working. A running `sai mcp serve`
keeps the old code until its client restarts; `mcp_info` reports
`restartNeeded`.

With `update.auto` on, which is the default, sai also updates by itself. A
command run in a terminal starts `sai update --background` detached, at most
once a day, and does not wait for it. A later command prints one line on
stderr when the update finished or failed. With `update.auto` off, the
background run only checks, and the notice says a release is available.
Nothing is started or printed under `--json`, when `CI` is set, without a
terminal, or by `sai mcp`, `sai update`, `sai doctor`, `sai config`,
`sai logs`, and `sai skill`. So `sai config set update.auto false` never
starts an update, and a background run reads the setting again right before
it installs. On Windows the background run only notifies.

`$SAI_HOME/update.json` holds the last check and the last update result, and
`update.lock` guards it. `sai doctor` reads it for its `version` check, which
warns when sai is behind or the last update failed. Two files sit beside the
tool's environment in uv's tool directory, shared by every `SAI_HOME` that
uses the same `sai`: `.sai-cli-update.lock`, held for a whole install, and
`.sai-cli-release.json`, which records the release, the wheel's SHA-256, and
a hash of the installed files. An install from a local wheel counts as a
release install only while that record matches what is installed.

These checks guard against a bad release, a failed network, two commands
racing, and mistakes such as uv pointed at another directory. They are not a
defense against someone who can already write to uv's tool directory or to
`$SAI_HOME`; that person can replace `sai` outright.

## Connecting

The CLI trades with a Keeper session token, the same HS256 JWT Sai Web keeps
in the browser. Keeper issues it for 30 days in production. It authorizes
every trade kind on the account's deposited funds, and Keeper cannot revoke it
yet: signing out of Sai Web clears only the browser copy, not the CLI's.

`auth import` stores a token copied from Sai Web. The **Connect Sai CLI /
MCP** button that copies it is in review
(https://github.com/SaiDotFun/sai-web/pull/1591); until it ships, use
`auth login NAME` below.

- With a terminal, it asks for the token at a hidden prompt. Press Enter
  without typing to read the clipboard.
- Without a terminal, as when an agent runs it, it reads the clipboard with the
  first command that works: `pbpaste`, `wl-paste`, `xclip`, or `powershell.exe
  Get-Clipboard` (WSL and Windows). If none works, as over SSH, it exits
  non-zero and asks for the person to run `sai auth import` in their own
  terminal.
- It never reads the token from arguments, environment variables, or piped
  stdin, and never prints it. Like `auth login` and `auth status`, it prints
  only `addr`, `expiresAt`, and `endpoint`.
- After storing the token, it reads the clipboard again and clears it only if
  it still holds the token. Anything copied in the meantime stays.

`auth login` signs Keeper's challenge with a local key. Before anything
signs, the CLI checks that the typed data has primary type
`UserTradingSession`, domain `Sai` version `1`, your addr, and the issued
challenge nonce.

`auth login NAME` picks the key named `NAME`:

- If the `ud` tool is on `PATH` and `ud evm wallet show NAME` reports a
  wallet whose Cast keystore is `matching`, the CLI signs with `ud evm
  wallet run NAME -- wallet sign --data --from-file PATH`, which needs no
  prompt. ud holds the password for the keystore of the same name. If ud
  fails to sign, for example because the keystore has another password, the
  CLI signs with `cast` instead when run in a terminal, and `cast` asks for
  the password.
- Otherwise it uses the Foundry keystore `~/.foundry/keystores/NAME`. It
  reads the addr from the keystore file (or `cast wallet address --account
  NAME` if the file has none) and runs `cast wallet sign --account NAME
  --data --from-file PATH` on your terminal, where `cast` prompts for the
  keystore password.
- If ud and the keystore hold different accounts under `NAME`, it stops
  before signing and names both. `--addr` with the keystore's addr signs in
  as that account. ud cannot sign with its own until the keystore is renamed
  and `ud evm wallet sync NAME` has run.
- If `ud` is installed but its lookup fails, the CLI cannot tell whether ud
  holds another account under `NAME`, so it stops. `--addr` with the
  keystore's addr signs in as that account.
- With neither, it stops and names where it looked.

`--addr ADDR` is optional. The CLI refuses to sign when ud or the keystore
reports a different addr. For a keystore it reads the file's plaintext
`address` field, which needs no password, and falls back to `cast wallet
address` only when the file has none. That field is not bound to the
encrypted key: if someone edited it, the check passes, Keeper rejects the
signature, and nothing is stored.

### Several Accounts

Each `auth login` or `auth import` connects an account and makes it active.
Signing in with another account keeps the ones already connected; signing in
again with the same addr refreshes its session.

- `auth list` shows each connected account, its expiry, and which is active.
- `auth use ADDR` makes another connected account active.
- `--as ADDR`, before the command, runs one command as another connected
  account: `sai --as 0x... trade close 3`.
- `auth logout [ADDR]` removes one account, by default the active one.
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
or `sai auth login NAME` again. A single-account `session.json` from before
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
the session of the account it acts as (the active one, or its `addr`)
before it rebuilds the preview: no session returns
`SIGNED_OUT`, an expired one `SESSION_EXPIRED`, and one for another endpoint
or an unreadable file `SESSION_UNAVAILABLE`. Each message tells the agent to
have the user run `sai auth login NAME` or `sai auth import`. A signed-in trade
still needs the `previewDigest` of the preview the user confirmed.

## Commands

Commands include `market list`, `market prices`, `account positions ADDR`,
`account balances ADDR`, `account history ADDR`, `trade open`, `trade
close`, `trade partial-close`, `trade tpsl`, `intent status ADDR --key KEY`,
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
`intent status ADDR --key KEY`. If Keeper has no record of the key five
minutes after submission, past Keeper's two-minute admission expiry, the CLI
marks it `absent` and trading resumes. A connection that fails before the
request is sent marks the intent `not_sent`, which does not block. Keeper
admission does not establish execution or finality.

`sai doctor` checks the install, the version, `$SAI_HOME/trader`, the session, the intent
ledger, and the Keeper HTTP, WebSocket, and LCD endpoints, and fixes what it
safely can. It creates `$SAI_HOME/trader`, removes group and other access
from it and the files sai keeps there (the connected accounts, the ledger and its
journals, and the shell history), migrates the intent ledger, and reconciles
unknown intents for the signed-in account the way `sai intent status` does
(a Keeper read; it never places or cancels a trade). It never deletes or
rewrites the session, never writes through a symlink in `$SAI_HOME/trader`,
and never prints the session token. Without fixes it reads the ledger from a
temporary copy, so it writes nothing at all.

Each check prints pass, fixed, warn, fail, or skipped, with what changed or
the command that fixes it. Doctor exits 1 if any check still fails;
`sai --json doctor` prints the same report as JSON. `--no-fix` reports
without changing anything, and `--offline` skips the network checks, which
otherwise run in parallel and stop after 7 seconds in total. The
endpoints come from [Settings](#settings); `sai doctor --ws-endpoint` and
`--lcd-endpoint` override them for one run.

Run `sai` with no arguments for an interactive shell. `help` or `help COMMAND`
prints usage, Ctrl-C cancels the running command, and Ctrl-D or `exit` leaves.
In a terminal, the shell keeps command history in `$SAI_HOME/trader/history`.

`utils/repl_skin.py` is adapted from CLI-Anything
(https://github.com/HKUDS/CLI-Anything) under the Apache License 2.0. See the
repository license.
