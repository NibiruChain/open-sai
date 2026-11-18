---
name: sai
description: Inspect Sai markets and positions, follow live updates, and submit authenticated trading intents using the Sai CLI.
metadata:
  cli-version: "0.1.0"
---

# Sai CLI

Always run `sai --skill` first and follow its output over any other copy of
this file. It prints the skill shipped with the installed CLI, works offline,
and stays correct when two builds report the same `sai --version`. The
installed `sai --help` is the authority for commands and options.

Use `sai --help` to inspect commands. Add `--json` before the
command for machine-readable output. Mainnet is the default. Override the
Keeper HTTP endpoint with `--endpoint`; WebSocket commands derive their endpoint
from it unless `--ws-endpoint` is supplied.

Use `market list`, `market prices`, `account positions ADDR`, `account
balances ADDR`, and `account history ADDR` for data. Use `watch price
TOKEN_ID`, `watch positions ADDR`, or `watch balances ADDR` for streams.
Streams reconnect after transport interruptions and emit a new snapshot.

## Connect Sai

When the person asks you to connect Sai:

1. Ask them to copy their session token with the **Connect Sai CLI / MCP**
   button in Sai Web. If their Sai Web has no such button yet, ask them to
   run `sai auth login NAME` in their own terminal instead
   (below).
2. Run `sai --json auth import`. Without a terminal it reads the clipboard,
   stores the token, clears the clipboard, and prints `addr`,
   `expiresAt`, and `endpoint`.
3. If your commands run in a pseudo-terminal, `auth import` shows a hidden
   prompt instead. Send only an empty line (Enter), which makes it read the
   clipboard. Never type, paste, or echo the token yourself.
4. If it says no clipboard command works (for example over SSH), ask the
   person to run `sai auth import` in their own terminal, then continue.

Never ask the person to paste the token into the chat, and never pass it as an
argument, environment variable, or piped input. `auth import` does not accept
those.

People with a Foundry keystore can instead run `sai auth login NAME` in their
own terminal, where `NAME` is the keystore. `cast` prompts them for the
keystore password, unless the `ud` tool has a wallet of that name, which
signs without a prompt.

`--addr` is an optional cross-check; leave it out unless the person gives
one.

Once connected, commands need no prompt until the session expires. `auth
status` shows the active account's addr and expiry without revealing the
token.

The person can connect several accounts; each sign-in adds one and makes it
active. `auth list` shows them and which is active. `auth use ADDR`
switches the active account; switch only when the person asks. To act on
another connected account for one command, put `--as ADDR` before the
command instead. Before a trade, say which account it will use. `auth logout
[ADDR]` removes one account, by default the active one; it does not revoke
the token. Tell the
person what the token is if they ask: it is their Sai Web browser session
token, lasts as long as Keeper issues it (30 days in production), authorizes
all trading on their deposited funds, and cannot be revoked yet. Signing out
of Sai Web does not sign out the CLI.

The CLI encrypts the stored session with a key built into the CLI and never
prompts for a passphrase. If it reports a passphrase-encrypted session from an
older version, ask the person to sign in again with `sai auth import` or `sai
auth login`.

## MCP Server

If Sai tools such as `mcp_info` and `market_prices` are available, prefer
them over shell commands. Call `mcp_info` first. Trade tools exist only when
the server runs with `SAI_MCP_EXPOSED_TOOLS=all`. Before any trade, call the
matching `preview_*` tool, show the person the result, and wait for explicit
confirmation. Then call the `trade_*` tool with the preview's `previewDigest`
(and `referencePrice` for a market open). On `PREVIEW_MISMATCH` or
`PRICE_MOVED`, show the fresh preview and confirm again. After a timeout or
unknown outcome, call `intent_local` and `intent_status`; never resubmit
blindly.

`auth_list` shows the connected accounts and which is active. Preview and
trade tools act on the active account, or on another connected account
passed as `addr`. Name the account when you show a preview. Call
`auth_use` only when the person asks to switch; it also changes the CLI's
active account.

## Trading

Trade commands are `trade open`, `trade close`, `trade partial-close`, and
`trade tpsl`, `trade leverage`, and `trade margin`. Use each command's `--help` for parameters. `--dry-run` prints
the local request without submitting an intent. Accepted means Keeper admitted
the request; it does not mean the trade executed. Use `intent local ADDR`
and `intent status ADDR --key KEY` to inspect and reconcile. If the outcome
is unknown, retain its intent key and do not blindly submit again. An unknown
intent blocks new trades until `intent status --key` resolves it; when Keeper
has no record five minutes after submission, the CLI marks it `absent` and
trading resumes.

## Examples

These examples use placeholders for the public account addr and an
existing trade ID. They do not fund an account or send a trade.

```bash
sai --json market list
sai --json account positions 0xYOUR_ADDR
sai --json account balances 0xYOUR_ADDR
sai auth import
sai --json trade open --market 0 --side long --type market \
  --amount 2 --leverage 2 --slippage 1 --dry-run
sai --json trade open --market 0 --side long --type limit \
  --amount 2 --leverage 2 --price 60000 --slippage 1 --dry-run
sai --json trade partial-close 42 --percent 50 --dry-run
sai --json trade leverage 42 --leverage 5 --dry-run
sai --json trade margin 42 --direction add --amount 5 --dry-run
sai --json trade close 42 --dry-run
sai --json intent status 0xYOUR_ADDR --key INTENT_KEY
sai --json watch price 3
```

For a live market order, the CLI refreshes the market price before admission
and includes the reference price in the accepted response. A dry run reports
that price refresh is still required. Partial close reads live position,
contract, and market data, including in dry-run mode, and refuses while Keeper's
index lags the contract. A successful execution remains pending
locally until the CLI observes matching indexed trade history.

Margin commands quote from live contract state even in dry-run mode. They
submit a target leverage rather than an amount cap, so the actual margin delta
may differ if the position changes between the last check and execution.
