# poe2market — data model

## Where prices come from

Two sources, each for what it does well:

| Data | Source | Why |
|---|---|---|
| **Currency** (orbs, omens, essences, uncut gems, …) | **poe.ninja** | Reflects the in-game Currency Exchange, where PoE2 currency actually trades. The GGG trade2 exchange reads a near-dead bulk market (single-digit offer counts) |
| **Items / uniques** | GGG **trade2 search** | Returns hundreds of real listings; the right tool for gear |

Currency is pulled from poe.ninja hourly (matching its refresh rate) and
stored with `source='ninja'`. `currency_rate` prefers ninja samples over any
older trade2 `exchange` rows. poe.ninja prices come denominated in Divine Orbs
and are converted to the base currency via the exalted rate in the same feed.

Currency prices are inherently up to ~1 hour stale (poe.ninja refreshes
hourly) — fine for valuation, verify live before a real trade.

## Storage tiers

Raw ticks reach ~46M rows/year, so charts never read them.

| Table | Retention | Purpose |
|---|---|---|
| `price_sample` | 14 days | Raw observations; recent detail, arbitrage |
| `price_hourly` | forever | OHLC candles; charts under ~14 days |
| `price_daily` | forever | OHLC candles; league-long charts, movers |
| `listing` | 14 days | Individual offers; whisper strings, depth |
| `stash_snapshot` / `stash_item` | forever | Valued inventory over time |

`get_price_history(resolution="auto")` picks hourly under 14 days, daily beyond.

## Price semantics

Everything is normalised to a **base currency** (default `exalted`).

| Field | Meaning |
|---|---|
| `price` / `price_base` | Value in the base currency. The charting value |
| `best_bid` | Highest price a buyer offers. What you receive selling |
| `best_ask` | Lowest price a seller asks. What you pay buying |
| `spread_pct` | `(ask - bid) / mid * 100`. Wide == illiquid |
| `depth` | Units available, both sides, in units of the priced item |

### A note on the old trade2 exchange

Earlier versions priced currency from GGG's `trade2/exchange`, sampling both
directions (`have=base,want=X` for asks; `have=X,want=base` for bids). That
endpoint turned out to read a near-abandoned market — a liquid currency showed
5 offers against poe.ninja's tens of thousands of trades — so currency now
comes from poe.ninja instead. The two-sided book logic remains for any
exchange-sourced rows but is no longer the currency source.

### Side-aware conversion

Converting a divine-priced item to exalted uses a different rate depending on
the question:

- **ask** — what you would pay. Costing a purchase.
- **bid** — what you would receive. Valuing a stash.
- **mid** — midpoint. Trend charts only.

With a 92% spread these differ by 2.7×, so the choice is not cosmetic.

## Junk rejection: finding the real split

The exchange returns rows that are real listings but meaningless prices. For
divine in one league: eight genuine bids of 140-230 exalted alongside thirteen
entries like *"gives 1 exalted, takes 10000 divine"*, plus a fat-fingered ask
of 100 when the market was ~190. **Junk was the majority**, so a median or a
trimmed mean lands squarely inside it.

Three steps, run before anything is measured or stored:

1. **Locate the market.** Take the largest tight cluster across both sides in
   log space. Genuine listings for one item group inside a factor of a few;
   junk sprays across four orders of magnitude. This survives junk being the
   majority, which rank-based methods do not.
2. **Find the real split.** Inside that cluster, take the closest ask/bid pair.
   Its midpoint is the anchor — the point where the two sides actually meet.
3. **Drop deviations.** Measure spread about the anchor with a median absolute
   deviation (robust, so survivors cannot skew it) and discard anything beyond
   `outlier_sigmas`, default **1.0**.

Measured effect on that divine book:

| σ | asks kept | spread |
|---|---|---|
| 1.0 | 188 | **15.5%** |
| 2.0 | 188, 260 | 32.7% |
| 3.0 | 100, 188, 260, 300 | 32.7% |

Rejected listings are **discarded, not stored** — they would otherwise have to
be filtered again on every read, and they inflate depth badly (junk stock
turned a real depth of a few hundred into 19 million). The same filter applies
to item searches, where bait listings — a chase unique at 1 exalted to farm
whispers — are the equivalent problem.

`n_asks_raw` / `n_bids_raw` record how many offers existed before filtering, so
the rejection rate stays visible.

## Sources

| `source` | Endpoint | Shape |
|---|---|---|
| `exchange` | `trade2/exchange` | Two-sided book, base currency |
| `search` | `trade2/search` + `fetch` | Ask-side percentiles, listed currency |

`low`/`high` mean **bid/ask** for `exchange` rows and **percentiles in the
listed currency** for `search` rows. Rollups guard against this: a search row's
candle uses only `price_base`, so divine-denominated listings never widen an
exalted candle.

## Candles

`open`/`close` are the first and last `price_base` in the bucket. `high`/`low`
widen to each sample's own bid/ask envelope for exchange rows — in a thin
market that range is often more informative than the trend.

`samples` is how many observations the bucket contains; a low count means the
candle is weakly supported.

## Direct pair rates

`pair_rate` stores direct currency-to-currency quotes, which exist for one
reason: **every other price is quoted against the base currency, so all
cross-rates are synthetic.** A cycle built from base-relative quotes pays the
spread on each leg and can never show a profit — it is arithmetically
impossible, not merely unlikely.

Genuine multi-hop arbitrage lives where the market's own direct chaos→divine
rate has drifted from chaos→exalted→divine. One request prices every pair
leaving a currency, so N currencies cost N requests to sweep completely.

`rate` is the median (the honest rate); `best_rate` is the most favourable
offer in the cluster (what one execution could achieve now).
