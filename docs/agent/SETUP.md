# Setup and operations

## Install

```bash
cd ~/POE2MarketMCP
uv venv && uv pip install -e .
poe2market init            # interactive: contact email + account handle
```

## Configure

Run `poe2market init` — it prompts for your contact email (GGG requires it) and
your account handle (optional, for stash), and writes `config/config.toml`. To
edit by hand instead, copy `config/config.example.toml` to `config/config.toml`.

**`contact_email`** — set this to an address you actually monitor. GGG's policy
requires a contact in the User-Agent; their alternative to emailing you about
problem traffic is a silent IP ban.

**`leagues`** — defaults to `["@current"]`, which resolves to the active
challenge league at runtime and follows league rollover without an edit. Use
`"@current-hc"` for hardcore, or an exact name to pin.

**`outlier_sigmas`** — how far from the real split a listing may sit before it
is discarded. Default `1.0`. Measured on live divine data, 1.0 and 1.5 both
give a 15.5% spread while 2.0 admits stale asks and doubles it to 32.7%.

## Run the collector

```bash
poe2market collect --once          # one pass of every due job
poe2market install-daemon          # run continuously via launchd
poe2market status                  # coverage and recent runs
tail -f logs/collector.err.log     # live log (Python logs to stderr)
poe2market uninstall-daemon        # stop and remove
```

The daemon restarts on crash (verified: ~11s) and at every login. It is a
**LaunchAgent**, so it starts when you log in, not at boot — a LaunchDaemon
would run as root and break Keychain access to your stash credential for no
benefit.

Watchlists and `config.toml` **hot-reload**: edits take effect on the next tick
without a restart, and existing job timers are preserved so a reload does not
stampede the API.

### Recovering from sleep, reboot, or a dropped connection

Collection resumes on its own:

- **Machine sleeps or goes offline** — jobs fall overdue and run within ~60s of
  waking, logged as `catching up, N late`.
- **Reboot** — the service starts at login and runs every due job immediately.
- **A fetch fails, or returns nothing** — the job retries on a short, capped
  schedule (30s doubling to 5 min) instead of waiting out its full cadence, so
  it resumes a minute or two after connectivity returns. Normal cadence is
  restored on the first success (`recovered after N failure(s)`).

A job that returns without error but collected nothing counts as a failure too —
an upstream hiccup should not cost a whole cadence of data.

## Watchlists

Currency is swept automatically. Named items live in
`config/watchlists/*.toml`, each with its own cadence and priority.

Three ways to specify a target, increasing in power:

```toml
[[target]]                          # 1. a named unique
key = "uniq:mageblood"
kind = "unique"
name = "Mageblood"
type = "Utility Belt"

[[target]]                          # 2. a base type, narrowed by filters
key = "base:stellar-amulet-i82"
kind = "base"
type = "Stellar Amulet"
[target.filters.misc_filters.filters.ilvl]
min = 82

[[target]]                          # 3. a raw trade2 query — any filter
key = "gear:wand-plus3-spell"       #    the trade site supports
kind = "raw"
[target.raw_query.query]
status = { option = "online" }
[target.raw_query.query.filters.type_filters.filters.category]
option = "weapon.wand"
[[target.raw_query.query.stats]]
type = "and"
[[target.raw_query.query.stats.filters]]
id = "explicit.stat_124131830"      # + to Level of all Spell Skills
[target.raw_query.query.stats.filters.value]
min = 3
```

Raw queries reach **8,296 stat ids** across `explicit`, `pseudo`, `implicit`,
`crafted`, `rune`, `enchant`, `fractured`, `desecrated`, `sanctum` and `skill`,
with `and` / `count` / `not` grouping. Omitting `type` searches every base,
which is usually what you want — the mod carries the value.

### Always validate

```bash
poe2market validate
```

Runs every target once and reports match counts. An over-constrained target
never errors — it silently records an empty series until you notice. This
caught a live target matching zero listings, and a `+3 spell & 50 life` wand
query that returned nothing because life on a wand is rare (the same query
without the life requirement matched 50).

Flags: `ok` / `THIN` (<3 listings) / `DEAD` (0).

## Direct pair rates

Needed only for `find_multi_step_arbitrage`. Set which currencies to sweep:

```bash
poe2market suggest-pairs
```

This ranks currencies by **measured two-sided liquidity** and prints a
recommended `pair_currencies` line. Do not guess the list: of 215 priced
currencies only 6 traded on both sides, and an ask-only currency cannot close a
loop. It also excludes anything whose value ratio makes a direct market
implausible — `mirror` had zero two-sided samples because its only listings are
bait.

The base currency must be present for routing comparisons.

Pair rates populate only once both currencies of a pair have a base price, so
this data accumulates more slowly than the currency series.

## Stash valuation

The tool reads your **public trade listings**, filtered by account name.

```toml
# config.toml — your handle WITH the discriminator
stash_account = "Name#1234"
```

or `export POE2MARKET_ACCOUNT="Name#1234"`. Then:

```bash
poe2market stash                     # market-priced, speculative gear excluded
poe2market stash --include-listed    # also count your self-set gear prices
```

### Exposing your stash

To make items visible, set the stash tab **public with a price** in game:

- Name the tab `~price 5 exalted` (or `~b/o 5 exalted`), or set the tab's price
  field. Items inherit the tab price and index even if not individually priced.
- A tab set "public" **without a price does not appear** — the price is what
  feeds trade search.
- **Log in to refresh.** GGG re-indexes your stash only when you are online; the
  data reflects its last crawl of your account.

Held or unpriced items, and anything in the in-game Currency Exchange, are not
visible. Currency is valued at live poe.ninja prices; gear (off by default) at
your own asking price.

If an item is missing, its tab almost certainly lacks a price — the single most
common cause.

## Installing on another machine

Nothing in `src/` or `config/` hardcodes a path, so the repo is portable.

```bash
git clone <your remote> POE2MarketMCP && cd POE2MarketMCP
uv venv && uv pip install -e .
poe2market init              # interactive setup (or set POE2MARKET_ACCOUNT etc.)
poe2market install-daemon    # launchd on macOS, systemd --user elsewhere
poe2market status
```

What is and is not portable:

| | Portable | Notes |
|---|---|---|
| Code and config | yes | Paths resolve from `POE2MARKET_HOME` |
| Account name | yes | Just a config value / env var |
| Collected history | **no** | `data/market.db`; copy it, or let each machine collect |
| Daemon | yes | launchd (macOS) or `systemd --user` (Linux) |

Both service types are **user** services on purpose, matching where the config
and data live.

Rate limits are enforced per **IP**, so two machines on one connection share a
budget the shared ledger cannot see. Either run the collector on one machine,
or lengthen the cadences on both.

If you want one history rather than several, run the collector on a single
always-on machine and point the others at that database — or copy
`data/market.db` across, since history cannot be regenerated.

## Register the MCP server

```json
{
  "command": "/path/to/POE2MarketMCP/.venv/bin/python",
  "args": ["-m", "poe2market.mcp.server"],
  "env": {"POE2MARKET_HOME": "/path/to/POE2MarketMCP"}
}
```

Goes in `~/Library/Application Support/Claude/claude_desktop_config.json`
(under `mcpServers`) and/or `~/.claude.json`. Restart the client afterwards —
config is read at launch.

The MCP server is **not** a long-running process: it is spawned per client and
exits on disconnect. Only the collector runs continuously, which is why data
collection is unaffected by whether any client is open.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `get_price` returns `found: false` | Not collected yet. Check `market_status` |
| History empty or tiny | Rollups run every 15 min; history starts at the collector's first run and cannot be backfilled |
| `find_multi_step_arbitrage` → `ok: false` | No `pair_rate` data yet — see above |
| A watchlist target records nothing | Over-constrained. Run `poe2market validate` |
| A stash item is missing | Its tab has no buyout price, so GGG won't index it |
| Collector silent | `launchctl list com.igorchernyy.poe2market.collector`; logs in `logs/collector.err.log` |
| Config edit ignored | Top-level TOML keys must come **before** any `[[table]]` section, or they parse into the last table |
