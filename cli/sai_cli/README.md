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

`sai mcp serve` runs the stdio MCP server for agent clients, for example
`claude mcp add sai -- sai mcp serve`. A bare `sai mcp` prints help. The
server writes only MCP messages to stdout and logs to stderr; run on a
terminal, it prints one stderr line saying an agent client should launch it.
`--endpoint`, `--ws-endpoint`, and `--lcd-endpoint` pick the network as the
top layer of [Settings](#settings), falling back to the same `sai` global
flags, then `SAI_ENDPOINT`, `SAI_WS_ENDPOINT`, and `SAI_LCD_ENDPOINT`, then
the config file, then mainnet. They resolve once, at start.

It lists every tool regardless of sign-in. Each `trade_*` call checks the
session of the account it acts as (the active one, or its `address`) before
it rebuilds the preview: no session returns
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

Run `sai` with no arguments for an interactive shell. `help` or `help COMMAND`
prints usage, Ctrl-C cancels the running command, and Ctrl-D or `exit` leaves.
In a terminal, the shell keeps command history in `$SAI_HOME/trader/history`.

`utils/repl_skin.py` is adapted from CLI-Anything
(https://github.com/HKUDS/CLI-Anything) under the Apache License 2.0. See the
repository license.
