# Test Plan

- Exercise the real Click command through its installed entry point and the
  `CliRunner` interface with a local GraphQL test server.
- Verify market and position queries, accepted and uncertain submissions,
  retained intent identity, status reconciliation, and argument validation.
- Verify encrypted credential round trips, incorrect passwords, and restrictive
  file permissions without using real credentials.
- Verify WebSocket transport with a local protocol fixture.
- Do not submit live trades or acquire a real wallet session in automated tests.

## Local Results

`CLI_ANYTHING_FORCE_INSTALLED=1 uv run pytest -q` passed 24 tests on
2026-11-13, including installed-command HTTP and WebSocket fixtures. These
fixtures do not prove a live trade. `just check` passed Ruff and ty.
Margin tests cover exact integer rounding, stale position detection, balance
checks, and network mismatch. A read-only mainnet LCD contract balance query
also succeeded; margin and leverage mutations have not been tested on mainnet.

## Mainnet Verification

On 2026-11-13, an authorized manual test signed in with an EVM wallet, opened
a BTC market position with $2 collateral at 2x leverage, closed 50%, and closed
the remainder. Keeper's indexed trade history showed matching open,
size-decrease, and close events. The final position query showed no open
positions for the test account. This validates those observed actions, not all
order types or all market conditions.
