---
name: sai
description: Inspect Sai markets and positions, follow live updates, and submit authenticated trading intents using the Sai CLI.
---

# Sai CLI

Use `sai --help` to inspect commands. Add `--json` before the
command for machine-readable output. Mainnet is the default. Override the
Keeper HTTP endpoint with `--endpoint`; WebSocket commands derive their endpoint
from it unless `--ws-endpoint` is supplied.

Use `market list`, `market prices`, `account positions ADDRESS`, `account
balances ADDRESS`, and `account history ADDRESS` for data. Use `watch price
TOKEN_ID`, `watch positions ADDRESS`, or `watch balances ADDRESS` for streams.
Streams reconnect after transport interruptions and emit a new snapshot.

Use `auth import` to provide an existing session through a hidden prompt.
`auth login --address ADDRESS --wallet NAME` is experimental and works only on
workstations with the private `ud` wallet manager.
The encrypted local token expires according to Keeper's session. `auth status`
shows the address and expiry without revealing credentials. Write commands ask
for the local passphrase, or read it from an inherited file descriptor given to
the global `--passphrase-fd` option. Never put a token or passphrase in command
arguments, environment variables, output, or logs.

Trade commands are `trade open`, `trade close`, `trade partial-close`, and
`trade tpsl`, `trade leverage`, and `trade margin`. Use each command's `--help` for parameters. `--dry-run` prints
the local request without submitting an intent. Accepted means Keeper admitted
the request; it does not mean the trade executed. Use `intent local ADDRESS`
and `intent status ADDRESS --key KEY` to inspect and reconcile. If the outcome
is unknown, retain its intent key and do not blindly submit again. An unknown
intent blocks new trades until `intent status --key` resolves it; when Keeper
has no record five minutes after submission, the CLI marks it `absent` and
trading resumes.

## Examples

These examples use placeholders for the public account address and an
existing trade ID. They do not fund an account or send a trade.

```bash
sai --json market list
sai --json account positions 0xYOUR_ADDRESS
sai --json account balances 0xYOUR_ADDRESS
sai auth import
sai --json trade open --market 0 --side long --type market \
  --amount 2 --leverage 2 --slippage 1 --dry-run
sai --json trade open --market 0 --side long --type limit \
  --amount 2 --leverage 2 --price 60000 --slippage 1 --dry-run
sai --json trade partial-close 42 --percent 50 --dry-run
sai --json trade leverage 42 --leverage 5 --dry-run
sai --json trade margin 42 --direction add --amount 5 --dry-run
sai --json trade close 42 --dry-run
sai --json intent status 0xYOUR_ADDRESS --key INTENT_KEY
sai --json watch price 3
```

For a live market order, the CLI refreshes the market price before admission
and includes the reference price in the accepted response. A dry run reports
that price refresh is still required. Partial close reads live position and
market data, including in dry-run mode. A successful execution remains pending
locally until the CLI observes matching indexed trade history.

Margin commands quote from live contract state even in dry-run mode. They
submit a target leverage rather than an amount cap, so the actual margin delta
may differ if the position changes between the last check and execution.
