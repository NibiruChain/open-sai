---
name: cli-anything-sai
description: Inspect Sai markets and positions, follow live updates, and submit authenticated trading intents using the Sai CLI.
---

# Sai CLI

Use `cli-anything-sai --help` to inspect commands. Add `--json` before the
command for machine-readable output. Mainnet is the default. Override the
Keeper HTTP endpoint with `--endpoint`; WebSocket commands derive their endpoint
from it unless `--ws-endpoint` is supplied.

Use `market list`, `market prices`, `account positions ADDRESS`, `account
balances ADDRESS`, and `account history ADDRESS` for data. Use `watch price
TOKEN_ID`, `watch positions ADDRESS`, or `watch balances ADDRESS` for streams.
Streams reconnect after transport interruptions and emit a new snapshot.

Use `auth login --address ADDRESS --wallet NAME` with the optional `ud` wallet
manager, or `auth import` to provide an existing session through a hidden prompt.
The encrypted local token expires according to Keeper's session. `auth status`
shows the address and expiry without revealing credentials. Write commands ask
for the local passphrase, or read it from an inherited file descriptor given to
the global `--passphrase-fd` option. Never put a token or passphrase in command
arguments, environment variables, output, or logs.

Trade commands are `trade open`, `trade close`, `trade partial-close`, and
`trade tpsl`. Use each command's `--help` for parameters. `--dry-run` prints
the local request without submitting an intent. Accepted means Keeper admitted
the request; it does not mean the trade executed. Use `intent local ADDRESS`
and `intent status ADDRESS --key KEY` to inspect and reconcile. If the outcome
is unknown, retain its intent key and do not blindly submit again.
