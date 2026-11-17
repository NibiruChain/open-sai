# Open Sai

Open Sai is a public home for Sai tools and selected development work. The
first project is a trader CLI for Sai's market data and intent-based trading.

## Trader CLI

The `sai` command provides market and account queries, live data,
and trading intents. It uses Sai Keeper's mainnet endpoint by default. Fund the
Sai account through Sai Web before trading; depositing and bridging funds are
outside the CLI.

Install [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer, then run:

```bash
uv sync
uv run sai --help
uv run sai --json market list
uv run sai --json market prices
```

To install the command into your user tool directory from the checkout, run
`uv tool install .`. Then run `sai --help` without `uv run`.

For command examples, setup details, and recovery behavior, read the
[CLI guide](cli/sai_cli/README.md) and the
[agent skill](skills/sai/SKILL.md). A trade accepted by Keeper is
still pending until its outcome is observed. Use `--dry-run` to inspect a local
trade request without submitting it.

### Connect to Sai

Trading needs a Sai session. There are two ways to get one:

- **From Sai Web.** Copy your session token in Sai Web (a **Connect Sai CLI /
  MCP** button is planned), then run `sai auth import`. In a terminal, paste
  the token at the hidden prompt or press Enter to read the clipboard. You can
  also tell your agent "connect Sai"; it runs `sai auth import`, which reads
  the clipboard when there is no terminal.
- **With a Foundry keystore.** Run `sai auth login --account NAME`. The CLI
  fetches Keeper's sign-in challenge, checks that it is a Sai trading-session
  request for your address, and runs `cast wallet sign`, which asks for the
  keystore password. Pass `--address` to skip the extra password prompt from
  `cast wallet address`.

After connecting, commands run without prompts until the session expires. Know
what the token is:

- It is the same session token Sai Web uses in your browser.
- It lasts as long as Keeper issues it: 30 days in production.
- It authorizes all trading on the funds deposited in your Sai account.
  Withdrawals still need your wallet.
- It cannot be revoked yet. Signing out of Sai Web does not sign out the CLI.

By default (`--keyring-backend test`), the CLI stores the session encrypted
with a key built into the CLI, like the Cosmos SDK `test` keyring. That keeps
the token out of plaintext files but does not protect it from anyone who can
read your files. Use `--keyring-backend file` (or set `SAI_KEYRING_BACKEND`)
to encrypt it with a passphrase instead.

## Development

Run `just install`, `just test`, and `just check`.

Install the agent skill with
`npx skills add SaiDotFun/open-sai --skill sai -g -y`. The repository copy at
`skills/sai/SKILL.md` is the source; the package ships it through the symlink
`cli/sai_cli/skills/SKILL.md`.
