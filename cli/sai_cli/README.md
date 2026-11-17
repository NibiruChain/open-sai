# Sai CLI

Install `sai` as the [root README](../../README.md) shows. From a checkout, run
`uv sync`, then `uv run sai --help`.
The CLI connects to mainnet Keeper by default and accepts `--endpoint` for a
different network. `account balances` and `watch` accept `--ws-endpoint` if the
WebSocket service differs from the HTTP endpoint.

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

`auth logout` deletes the stored session. It makes no network call, and since
Keeper cannot revoke tokens yet, a copy of the token elsewhere, such as in Sai
Web, stays valid until it expires.

The session is bound to its account and Keeper endpoint and expires with the
token. The CLI writes `$SAI_HOME/trader/session.json` with mode 0600,
encrypted by AES-GCM with a fixed key built into the CLI, like the Cosmos SDK
`test` keyring. Nothing prompts, so agents can run authenticated commands.
This only keeps the token out of plaintext; anyone who can read the file and
this source can decrypt it. Older versions could encrypt the session with a
passphrase. The CLI refuses those files and asks you to run `sai auth import`
or `sai auth login` again.


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
