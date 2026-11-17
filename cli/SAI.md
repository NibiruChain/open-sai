# Sai CLI Design

Sai Keeper exposes market and account data over GraphQL and accepts semantic
trading intents authenticated with a wallet session JWT. This CLI constructs
those requests directly. The client defaults to mainnet and accepts explicit
HTTP and WebSocket endpoints for local development and tests.

One command submits one intent. Before submission, the CLI records its identity
locally. A transport error can follow acceptance, so the CLI reports an unknown
outcome and requires reconciliation rather than retrying a new intent. Keeper
expires recorded intents after two minutes, so `intent status` treats a key
that Keeper has not recorded five minutes after submission as never admitted
and marks it `absent`. Keeper's acceptance is not trade finality. `intent status` reads Keeper's current state.

The browser issues short-lived JWTs after a wallet challenge. Device credential
provisioning is not yet part of this repository. The initial import command reads
a token and passphrase from hidden prompts and stores the token encrypted locally.
