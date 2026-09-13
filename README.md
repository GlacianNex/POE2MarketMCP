# POE2MarketMCP

Local Path of Exile 2 currency prices, price history, public-listing valuation,
and live trade searches for an MCP client.

Two processes share a local SQLite database: the **collector** builds history
while your machine is awake, and your **MCP client** starts the server to answer
questions. Connecting the MCP server does not start the collector.

Currency collection uses poe.ninja hourly by default. Optional item watchlists,
exchange sweeps, and live listing/stash lookups use GGG's trade API. League and
catalog discovery also contact GGG. Data is stored locally; tool results are
returned to your connected client.

## First-time setup

You need Python 3.11+ and `uv`, plus a contact email for API requests. The
background service supports macOS and Linux with systemd. No game login,
API key, or session cookie is needed for public listings.

From your existing checkout (or clone this repository first):

```bash
cd /path/to/POE2MarketMCP
uv venv --python 3.11
uv pip install --python .venv/bin/python -e .
.venv/bin/poe2market init
.venv/bin/poe2market collect --once
.venv/bin/poe2market status
.venv/bin/poe2market price cur:divine
```

Replace `/path/to/POE2MarketMCP` with your actual folder. Explicit `.venv/bin/`
commands work without activating the environment. If `init` says a config
already exists, edit `config/config.toml`; do not overwrite it just to continue.

**Success looks like:** `status` shows a league, nonzero samples, and a recent
`ninja` run with status `ok` and currencies priced. A successful command exit
alone does not prove collection worked. History starts with your first samples
and cannot be backfilled by this collector.

Then keep collection running:

```bash
.venv/bin/poe2market install-daemon
```

Connect your MCP client using the absolute Python path and `POE2MARKET_HOME`
in the [step-by-step setup guide](docs/agent/SETUP.md#3-connect-your-mcp-client).
That guide also covers prerequisites, logs, existing installations, and recovery.

## Try it

Once connected, ask your client:

- “Check market status and tell me how fresh the data is.”
- “Find the Divine Orb item key, then show its latest price and currency unit.”
- “Show Divine Orb history for the last day, using only the history collected.”
- “Value my public priced listings.” (Requires an account handle.)

Prices have source and freshness information; historical coverage depends on
how long collection has run. Stash valuation sees **public priced listings**,
not everything you own. Listing tools return whisper text for you to use
manually; they do not send messages or execute trades.

## Optional features

All bundled watchlists and GGG currency tiers are disabled by default;
`pair_currencies` is empty. Basic currency prices need none of these.
Enable and validate only the item targets you want to track. Those scans spend
GGG's trade budget, shared with other activity on your IP address.

See [watchlists and direct pairs](docs/agent/SETUP.md#optional-item-watchlists-and-direct-pairs)
and [stash setup](docs/agent/SETUP.md#optional-public-listing-valuation).

## Documentation and development

Start with the [documentation index](docs/README.md). The
[tool reference](docs/agent/TOOL_REFERENCE.md) describes tool arguments and
results; the [agent guide](docs/agent/AGENT_GUIDE.md) explains how to interpret
them. These documents are also served as MCP resources.

To install development tools and run the offline test suite:

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q
```

Architecture and maintenance notes are in [DESIGN.md](docs/maintainers/DESIGN.md).
MIT — see [LICENSE](LICENSE).
