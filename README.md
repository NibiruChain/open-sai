# Open Sai

Open Sai is a public home for Sai tools and selected development work. The
first project is a trader CLI for Sai's market data and intent-based trading.

## Trader CLI

The `sai` command provides market and account queries, live data,
and trading intents. It uses Sai Keeper's mainnet endpoint by default. Fund the
Sai account through Sai Web before trading; depositing and bridging funds are
outside the CLI.

Install [uv](https://docs.astral.sh/uv/), then run the CLI without
installing it:

```bash
uvx --from git+https://github.com/SaiDotFun/open-sai sai --json market prices
```

To install `sai` on your `PATH`, run:

```bash
uv tool install git+https://github.com/SaiDotFun/open-sai
sai --help
```

Upgrade with `uv tool upgrade sai-cli`, or rerun the install command with
`--reinstall`. Both Git commands install from `main`. `sai --version`
prints the installed version. The package is `sai-cli` and is not on PyPI
yet, so `uv tool install sai` installs an unrelated PyPI package.

Agents: run `sai --skill` to print the agent skill that matches the installed
version. Agents that load skills ahead of time can install it with
`npx skills add SaiDotFun/open-sai --skill sai`.

To work from a checkout, run `uv sync`, then `uv run sai --help`.

For command examples, setup details, and recovery behavior, read the
[CLI guide](cli/sai_cli/README.md) and the
[agent skill](skills/sai/SKILL.md). A trade accepted by Keeper is
still pending until its outcome is observed. Use `--dry-run` to inspect a local
trade request without submitting it.

### Connect to Sai

Trading needs a Sai session. There are two ways to get one:

- **From Sai Web.** A **Connect Sai CLI / MCP** button that copies your
  session token is coming to Sai Web
  ([sai-web#1591](https://github.com/SaiDotFun/sai-web/pull/1591)). Once it
  ships, copy the token and run `sai auth import`. In a terminal, paste the
  token at the hidden prompt or press Enter to read the clipboard. You can
  also tell your agent "connect Sai"; it runs `sai auth import`, which reads
  the clipboard when there is no terminal.
- **With a Foundry keystore.** This works today. Run
  `sai auth login --account NAME`, where `NAME` is a keystore in
  `~/.foundry/keystores`. The CLI fetches Keeper's sign-in challenge, checks
  that it is a Sai trading-session request for your addr, and only then
  runs `cast wallet sign`, which asks for the keystore password.

If you use the `ud` tool, `sai auth login --wallet NAME` signs with a ud
wallet instead, without a prompt.

You can connect several accounts. Each sign-in adds an account and makes it
active. `sai auth list` shows them, `sai auth use ADDR` switches the active
one, `sai --as ADDR ...` runs one command as another, and `sai auth logout
[ADDR]` removes one.

After connecting, commands run without prompts until the session expires. Know
what the token is:

- It is the same session token Sai Web uses in your browser.
- It lasts as long as Keeper issues it: 30 days in production.
- It authorizes all trading on the funds deposited in your Sai account.
  Withdrawals still need your wallet.
- It cannot be revoked yet. Signing out of Sai Web does not sign out the CLI,
  and `sai auth logout` only deletes the local copy.

The CLI stores sessions in `~/.sai/trader/sessions.json` with mode 0600,
encrypted with a key built into the CLI, like the Cosmos SDK `test` keyring.
That keeps the token out of plaintext files and grep, but anyone who can read
your files can decrypt it. There is no session passphrase.

## Use Sai From Your Agent (MCP)

The `sai-mcp` server gives MCP clients such as Claude Code the same reads,
previews, and trades as the CLI. Connect to Sai with the CLI first. The
server reads the stored session without a prompt, never asks for the token,
and never returns it.

```bash
claude mcp add sai -- uvx --from git+https://github.com/SaiDotFun/open-sai sai-mcp
```

From a checkout, use `uv run --directory /path/to/open-sai sai-mcp` as the
command instead.

The server starts read-only: market data, account reads, intent status, and
trade previews. To let the agent submit trades, set
`SAI_MCP_EXPOSED_TOOLS=all` before the `--`:

```bash
claude mcp add sai -e SAI_MCP_EXPOSED_TOOLS=all -- uvx --from git+https://github.com/SaiDotFun/open-sai sai-mcp
```

Each preview names the signed-in `account`, and each trade must carry the
`previewDigest` from a preview the user confirmed. Before submitting, the
server rebuilds the trade and refuses it with a fresh preview when it no
longer matches:

- Every trade: the signed-in account and the trade's arguments.
- `trade_margin` and `trade_partial_close`: also the position state the
  preview quoted.
- `trade_open` for a market order: also the market price, which may move at
  most the slippage from the confirmed `referencePrice`. A new price needs a
  new confirmation.
- `trade_close`, `trade_tpsl`, and `trade_leverage`: nothing beyond the
  account and arguments.

Ask the agent to call `mcp_info` to see the version, endpoints, mode, and
active account.

With several accounts connected, `auth_list` shows them, `auth_use` switches
the active one (for the CLI too), and preview and trade tools take an
optional `addr` to act as another connected account. A preview names its
account, and a trade refuses if the account changed after the preview. `SAI_ENDPOINT`, `SAI_WS_ENDPOINT`, and `SAI_LCD_ENDPOINT`
select another network.

## Development

Run `just install`, `just test`, and `just check`.

The repository copy at `skills/sai/SKILL.md` is the source of the agent skill;
the package ships it through the symlink `cli/sai_cli/skills/SKILL.md`, and
`sai --skill` prints it. When you bump the version in `pyproject.toml`, update
`metadata.cli-version` in the skill to match; a test checks this.
