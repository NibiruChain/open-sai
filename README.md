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

To trade, obtain a Keeper session with an EVM wallet using `auth login` and the
optional local `ud` wallet manager, or use `auth import` to enter an existing
session through a hidden prompt. The CLI encrypts the session locally with a
passphrase. Sessions expire; browser-to-device API credential provisioning and
per-device revocation are not implemented here.

For command examples, setup details, and recovery behavior, read the
[CLI guide](cli/sai_cli/README.md) and the
[agent skill](skills/sai/SKILL.md). A trade accepted by Keeper is
still pending until its outcome is observed. Use `--dry-run` to inspect a local
trade request without submitting it.

## Development

Run `just install`, `just test`, and `just check`.

Install the agent skill with
`npx skills add SaiDotFun/open-sai --skill sai -g -y`. The repository copy at
`skills/sai/SKILL.md` is the source; the package ships it through the symlink
`cli/sai_cli/skills/SKILL.md`.
