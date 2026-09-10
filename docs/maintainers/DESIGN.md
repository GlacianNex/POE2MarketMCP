# Design

Maintainer documentation: how the server is put together, why, and how to
extend it without breaking the guarantees it makes.

For *using* the server see [TOOL_REFERENCE](../agent/TOOL_REFERENCE.md) and
[SETUP](../agent/SETUP.md). Those two plus [AGENT_GUIDE](../agent/AGENT_GUIDE.md),
[DATA_MODEL](../agent/DATA_MODEL.md) and [RATE_LIMITS](../agent/RATE_LIMITS.md) are served to
connecting clients as MCP resources; this file and
[FINDINGS](FINDINGS.md) are deliberately not.

## Problem

Provide an MCP access point for Path of Exile 2 market data: current prices,
per-league price history, stash valuation, and trade preparation.

Three constraints shape everything, and all three were discovered by
measurement rather than assumed (see [FINDINGS](FINDINGS.md)):

1. **History cannot be backfilled.** GGG serves only *current* listings. Any
   price series exists only for the period something was running and recording.
   This makes the collector, not the server, the core of the system.
2. **The rate budget is small and shared.** 30 exchange requests per 300s per
   *IP*, and one request prices at most 10 currencies. Pricing all ~800
   exchangeable currencies costs ~160 requests per pass.
3. **The data is dirty in ways that break naive statistics.** Junk listings
   were observed as the *majority* of a currency's book, so medians and
   trimmed means land inside the junk.

## Architecture

```
                    ┌──────────────────────────────┐
                    │  pathofexile.com/api/trade2   │
                    └───────────────┬───────────────┘
                                    │  rate-limited, junk-filtered
                    ┌───────────────▼───────────────┐
                    │      collector daemon         │   launchd, 24/7
                    │  tiers · watchlists · pairs   │   survives reboot
                    │  rollups · retention          │
                    └───────────────┬───────────────┘
                                    │ writes
                    ┌───────────────▼───────────────┐
                    │        market.db (SQLite/WAL)  │
                    │  samples → hourly → daily      │
                    │  + shared rate-limit ledger    │
                    └───────────────┬───────────────┘
                                    │ reads
                    ┌───────────────▼───────────────┐
                    │         MCP server             │   spawned per client
                    │   14 tools · 6 resources       │   exits on disconnect
                    └────────────────────────────────┘
```

### Why two processes

The collector must run continuously; the MCP server is stdio and lives only as
long as its client. Fusing them would tie history collection to whether a chat
window happens to be open — the resulting gaps could never be repaired.

They share one database in WAL mode, so the collector writes while the server
reads without either blocking.

### Why the rate ledger is in SQLite

Limits are enforced per **IP**, but two processes share that IP. An in-process
limiter would let each spend the full allowance and collect a restriction. The
ledger of spent requests therefore lives in the database, and learned rate-limit
rules are persisted so a freshly started process knows the budget before its
first request.

## Key decisions

### Tiered collection, not a flat sweep

The 10-id cap makes a uniform sweep impossible. Currencies are split into tiers
with independent cadences, and the budget is spent where it matters:

| Tier | Contents | Cadence | Cost per 300s window |
|---|---|---|---|
| `fast` | the few orbs actually traded | 2 min | 5.0 |
| `core` | the `Currency` category | 10 min | 4.0 |
| `secondary` | fragments, essences, waystones… | 60 min | 4.3 |
| `long-tail` | everything else | 6 h | 1.4 |

Total ≈ 15 of 30 requests per window, leaving headroom for watchlists, pair
sweeps, and live tool calls.

A tier with an explicit `currencies` list claims those ids, so a fast-lane orb
is not re-swept by a broader tier below it.

### Both directions are sampled

The exchange endpoint is asymmetric: `have=base, want=X` returns sellers,
`have=X, want=base` returns buyers. Sampling one direction records "no market"
for currencies that trade fine. Double cost, correct data.

### Prices carry confidence, never a bare number

PoE2 books are thin and spreads of 90% occur. A midpoint quoted without that
context misleads anything trading on it, so every quote carries `spread_pct`
and a `confidence` label, and conversion is side-aware: buying uses the ask,
stash valuation uses the bid. The same item was worth 52k or 142k exalted
depending on side.

### Junk is rejected before measurement, and never stored

Three steps, because each alone is insufficient:

1. **Cluster** across both sides in log space — survives junk being the
   majority, which rank-based methods do not.
2. **Anchor** on the closest ask/bid pair inside that cluster: the real split,
   where the two sides actually meet.
3. **Drop** anything beyond `outlier_sigmas` (default **1.0**) of that anchor,
   using a median-absolute-deviation so survivors cannot skew the test.

Rejected listings are discarded rather than stored — they would otherwise be
re-filtered on every read, and they corrupt depth badly (junk stock turned a
real depth of a few hundred into 19 million).

Pairs whose value ratio exceeds 200× are never queried: a Mirror is ~100,000
Alchs, no direct market exists, and every listing on such a pair is bait.

### Three storage tiers

Raw ticks reach ~46M rows/year, so charts never read them.

| Table | Retention | Read by |
|---|---|---|
| `price_sample` | 14 days | recent detail, arbitrage |
| `price_hourly` | forever | charts under ~14 days |
| `price_daily` | forever | league-long charts, movers |

Rollups recompute whole buckets rather than incrementing, so a re-run is
idempotent and a crash mid-sweep needs no recovery logic.

### Direct pair rates are a separate collection

Every other price is quoted against one base currency, which makes all
cross-rates synthetic — and a cycle built from synthetic rates can **never**
profit, because its gain factorises into per-currency `bid/ask` ratios, each
below 1. This is arithmetic, not a data quality problem.

Genuine multi-hop arbitrage therefore requires *direct* quotes, collected
separately. Because a single pair often has only a handful of offers and all of
them can be junk, direct rates are validated against the base-relative prices —
a much better anchor, drawn from a two-sided book with more offers. Rates with
no available anchor are dropped rather than stored.

### Leagues resolve at runtime

PoE2 leagues carry no date metadata anywhere in the API. "Current" is defined
as the first entry in `trade2/data/leagues` that is neither permanent nor a
variant, relying on GGG listing the active challenge league first. The result
is cached, so a transient API failure cannot silently redirect collection into
the wrong league.

`leagues = ["@current"]` means the whole system follows league rollover with no
edit.

### Trade preparation stops at the whisper

No API executes a PoE2 trade, and automating in-game input violates GGG's
terms. Tools find listings, price them, rank them, check them against history,
and emit the exact whisper. A human sends it.

## Rejected alternatives

| Considered | Why not |
|---|---|
| poe2scout / poe.ninja as data source | Second-hand, and poe.ninja's PoE2 data loads through a client-side call not visible in its HTML. GGG is authoritative and directly reachable |
| Public stash river (`service:psapi`) | The sanctioned bulk path, but needs an OAuth client granted by GGG. Worth revisiting if usage outgrows personal scale |
| In-game Currency Exchange | Deeper and narrower-spread, but has no API surface at all |
| Cron/scheduled collection | Coarser granularity and gaps whenever the scheduler misses |
| Postgres / TimescaleDB | A database server for a single-user tool |
| Computing arbitrage from base prices alone | Provably impossible — see above |
| Median-based outlier rejection | Fails when junk is the majority, which it was |

## Known limitations

- **The trade site is not the whole economy.** `trade2` covers players' listed
  stash tabs. The in-game Currency Exchange carries most currency volume with
  far narrower spreads and has no API. This is why 209 of 215 currencies show
  one-sided markets on 1–2 offers. It *is* the slice you can buy from by
  whisper, which makes it right for trade preparation — but it should never be
  described as the whole market.
- **`api/trade2` is undocumented.** Not in GGG's API Reference; no stability or
  support guarantee. See [RATE_LIMITS](../agent/RATE_LIMITS.md#compliance-note).
- **Multi-hop arbitrage is unproven on live data.** The filters are correct and
  tested, but pair rates only populate once both currencies have a base price,
  and that had not accumulated at time of writing.
- **History starts now.** Nothing before the collector's first run exists.


---

# Working on this

## Code layout

```
src/poe2market/
  config.py            203   Settings + watchlist/tier models (pydantic)
  ratelimit.py         268   GGG rate-limit engine, SQLite-backed ledger
  ggg/
    client.py          158   HTTP, retries, learns limits from headers
    trade.py           592   Endpoint wrappers, junk rejection, MarketBook
    leagues.py         126   "@current" resolution + caching
  store/
    schema.sql         209   Tables, indexes, migrations-by-append
    db.py              666   All SQL. Nothing else talks to the database
  collect/
    daemon.py          242   Scheduler, hot-reload, signal handling
    jobs.py            407   Collection jobs (currency, watchlist, pairs)
    stash.py           304   Stash reading and valuation
  analysis/
    arbitrage.py       167   Cycle finding. Pure functions, no I/O
  mcp/
    server.py         1063   Tools + resources. Presentation only
  cli.py               476   Operator commands
tests/test_core.py     553   53 tests
```

**Layering rule:** `mcp/server.py` and `cli.py` are presentation. They must not
contain SQL or business logic — put queries in `store/db.py` and computation in
`analysis/`. The stash inventory query was moved out of the tool into
`Store.stash_items` for exactly this reason: logic inside a tool cannot be
tested without an MCP session.

## Invariants

Break these and the data silently degrades rather than failing loudly.

1. **Junk is rejected before measurement and never stored.** Any new price path
   must run through `resolve_market` / `dominant_cluster`. Storing raw offers
   means re-filtering on every read and corrupting depth.
2. **Never mix currencies.** `price_base` is in the base currency; `low`/`high`
   mean bid/ask for `exchange` rows and percentiles *in the listed currency*
   for `search` rows. The rollup has a `CASE` guard for this; keep it.
3. **All GGG calls go through `GGGClient.request`** with a bucket name, so the
   shared ledger sees them. A bare `httpx` call bypasses the budget and can
   earn an IP restriction that stops collection entirely.
4. **Rollups must stay idempotent.** They recompute whole buckets. A crash
   mid-sweep must never double-count.
5. **Failure must retry sooner, never later.** Exponential backoff on a job's
   full cadence strands it after an outage — a 30-minute job failing while the
   network is down would wait hours after it returns. Retries use a short capped
   interval, and a job returning no data counts as a failure.
6. **Charts read rollups, never raw samples.** Raw ticks are pruned after 14
   days; anything reading them breaks silently once retention kicks in.

## Adding things

**A tool** — decorate a function in `mcp/server.py` with `@server.tool()`. The
docstring becomes the description an agent reads, so state cost (local vs
live), what the return shape means, and any caveat. Add the row to
[TOOL_REFERENCE](../agent/TOOL_REFERENCE.md); a check in the test suite compares
documented tools against real ones.

**A collection job** — add a method to `Collector`, then register a
`ScheduledJob` in `build_jobs`. Jobs must be idempotent and must log a
`collector_run` row.

**A currency tier** — a `[[currency_tier]]` block in `config.toml`. Use
`currencies = [...]` for an explicit fast lane or `categories = [...]` for a
static-data category. Earlier tiers claim their ids, so ordering matters.
Budget: `ceil(n/10) * 2` requests per pass.

**A watch target** — a `[[target]]` in a `config/watchlists/*.toml`. Run
`poe2market validate` afterwards; an over-constrained query records an empty
series without erroring.

**A schema change** — append to `schema.sql` *and* add the column to
`Store._ADDED_COLUMNS`. `CREATE TABLE IF NOT EXISTS` is a no-op on an existing
database, so new columns need an explicit `ALTER`.

## Testing

```bash
pytest -q
```

Tests cover the logic that is easy to get quietly wrong: rate-limit header
parsing and cross-process budget sharing, junk rejection where junk is the
*majority*, rollup OHLC correctness and idempotency, the currency-unit
separation, league resolution, and arbitrage cycle detection.

Two conventions worth keeping:

- **Regression tests carry the real data that exposed the bug.** `REAL_BIDS` /
  `JUNK_BIDS` are the actual divine book observed live. Synthetic data would
  not have caught it — the first arbitrage test used invented rates and
  "passed" while reporting a fake 683% cycle.
- **Test the property, not the number.** `test_median_alone_would_have_failed`
  guards the *reason* the cluster step exists, so removing it fails loudly.

## Verifying against the live API

Behaviour worth checking after changes touching collection:

```bash
poe2market collect --once --job "currency:fast"   # one tier, ~2 requests
poe2market validate                               # watch targets still match
poe2market suggest-pairs                          # measured liquidity
poe2market status                                 # coverage, recent runs
```

Prefer `--job` over a bare `--once`: the `long-tail` tier is ~100 requests and
will saturate the budget for minutes.

Read the *output*, not just the exit code. Every bug in
[FINDINGS §11](FINDINGS.md#11-bugs-found-by-reading-output-not-by-testing) was
invisible to tests and obvious in one line of real output.
