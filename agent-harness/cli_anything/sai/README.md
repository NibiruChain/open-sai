# Sai CLI

Install dependencies with `uv sync`, then run `uv run cli-anything-sai --help`.
The CLI connects to mainnet Keeper by default and accepts `--endpoint` for a
different network. `account balances` and `watch` accept `--ws-endpoint` if the
WebSocket service differs from the HTTP endpoint.

`auth login --address ADDRESS --wallet NAME` uses the local `ud` wallet manager
to sign a Keeper challenge. `auth import` reads an existing session token from a
hidden prompt. Both protect the session at rest with a passphrase. The session
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
For a live market order, the CLI refreshes the price before admission; the
dry-run price is the supplied value. A partial close reads and rechecks the
position before submission. Market schedule and other Keeper admission checks
remain authoritative.

The CLI persists non-secret request identities under `$SAI_HOME/trader` or
`~/.sai/trader`. A network error after submission leaves the outcome unknown.
Reconcile the original intent before trying another action. Keeper admission
does not establish execution or finality.

This client uses the `ReplSkin` file from CLI-Anything, distributed under the
Apache License 2.0. See the repository license.
