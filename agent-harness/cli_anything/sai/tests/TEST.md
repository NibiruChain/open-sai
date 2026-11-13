# Test Plan

- Exercise the real Click command through its installed entry point and the
  `CliRunner` interface with a local GraphQL test server.
- Verify market and position queries, accepted and uncertain submissions,
  retained intent identity, status reconciliation, and argument validation.
- Verify encrypted credential round trips, incorrect passwords, and restrictive
  file permissions without using real credentials.
- Verify WebSocket transport with a local protocol fixture.
- Do not submit live trades or acquire a real wallet session in automated tests.
