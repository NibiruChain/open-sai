# Sai CLI

Install dependencies with `uv sync`, then run `uv run sai --help`.
The CLI connects to mainnet Keeper by default and accepts `--endpoint` for a
different network. `account balances` and `watch` accept `--ws-endpoint` if the
WebSocket service differs from the HTTP endpoint.

`auth import` reads an existing session token from a hidden prompt. Public
users should use it for now. `auth login --address ADDRESS --wallet NAME` is
experimental and workstation-only: it signs a Keeper challenge with the private
`ud` wallet manager, which is not publicly available. Both protect the session at rest with a passphrase. The session
is scoped to its account and endpoint and expires; device-specific credentials
and revocation are not implemented. For unattended processes, supply a private
inherited file descriptor with `--passphrase-fd`; do not pass the secret itself
as an argument or environment variable.

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
