# Test Plan

- Exercise the real Click command through its installed entry point and the
  `CliRunner` interface with a local GraphQL test server.
- Verify market and position queries, accepted and uncertain submissions,
  retained intent identity, status reconciliation, and argument validation.
- Verify encrypted credential round trips, incorrect passwords, and restrictive
  file permissions without using real credentials.
- Verify WebSocket transport with a local protocol fixture.
- Replay public mainnet market HTTP responses and a BTC WebSocket payload from
  saved fixtures, and compare synthetic account, auth, and trade messages with
  independently authored expected wire shapes. See [fixture provenance](fixtures/README.md).
- Do not submit live trades or acquire a real wallet session in automated tests.

## Local Results

Automated tests include installed-command HTTP and WebSocket fixtures. They
exercise recovery after a lost admission response across CLI restarts,
concurrent position reservations, a WebSocket disconnect and fresh snapshot,
and refusal of partial GraphQL errors. These fixtures do not prove a live
trade. `just check` runs Ruff and ty.
On 2026-11-13, `SAI_FORCE_INSTALLED=1 uv run pytest -q` passed 40 tests.
Routine tests use in-process stubs or local loopback HTTP and WebSocket servers;
they do not require an external Keeper connection or real credentials. A guard
blocks external TCP connections in the pytest process; subprocess fixtures use
explicit loopback endpoints.
Margin tests cover exact integer rounding, stale position detection, balance
checks, and network mismatch. A read-only mainnet LCD contract balance query
also succeeded.

## Mainnet Verification

On 2026-11-13, an authorized manual test signed in with an EVM wallet, opened
a BTC market position with $2 collateral at 2x leverage, closed 50%, and closed
the remainder. Keeper's indexed trade history showed matching open,
size-decrease, and close events. The final position query showed no open
positions for the test account. This validates those observed actions, not all
order types or all market conditions.

Further authorized mainnet checks opened a $4 position at 2x leverage, changed
leverage to 3x, and added $1 of margin. An immediate margin removal was rejected
as stale before an intent was submitted; the cause was not established. The
position was closed.

A second $4 position at 2x leverage successfully added $1 of margin. After a
12-second wait, removing $0.50 of margin and closing the position also
succeeded. The final position query showed no open positions for the test
account.

A final authorized check opened a BTC position, updated its stop loss, and
closed it. A separate BTC long limit order at 40,000 was created and canceled.
Keeper reported successful execution, and the final position query showed no
open positions for the test account.
