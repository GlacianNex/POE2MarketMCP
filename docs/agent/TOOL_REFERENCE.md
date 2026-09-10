# Tool reference

15 tools in four groups. Signatures below are the real schemas; every argument
not marked **required** has the default shown.

Omit `league` and the active challenge league is used (resolved at runtime, so
it follows league rollover on its own).

**Cost matters.** Local tools read the database: instant, free, unlimited.
Live tools call GGG and spend a budget *shared with the background collector* —
starving it leaves permanent holes in history, because GGG serves only current
listings and gaps can never be backfilled.

| | Local (free) | Live (spends budget) |
|---|---|---|
| Prices | `get_price`, `get_price_history`, `get_movers` | `find_listings`, `prepare_trade` |
| Discovery | `search_items`, `list_watchlists`, `market_status` | `list_leagues` |
| Analysis | `find_arbitrage`, `find_multi_step_arbitrage` | — |
| Stash | `list_stash_items`, `get_stash_value`, `get_stash_history` | `get_stash_value(refresh=true)` |

---

## Discovery

### `search_items(query="", kind="", watchlist="", limit=50)`

Resolve a user's wording to an **item key**. Other tools take keys, not names.

- `kind` — `currency` | `unique` | `base` | `raw`
- Keys look like `cur:divine` (currency) or `uniq:mageblood` (watchlist target)

```
search_items(query="Mage")
  -> {"count": 1, "items": [{"key": "uniq:mageblood", "label": "Mageblood", ...}]}
```

**Call this first** whenever the user names an item in prose.

### `list_leagues()` — *live*

Every league on the trade API, flagged with which are being collected. Use when
the user is vague ("the new league") so later calls pass an exact id.

### `market_status()`

Collector health, coverage window, and remaining rate budget.

**Call this before concluding that missing data means a missing market.** An
empty history nearly always means the collector has not run long enough.

### `list_watchlists()`

What is scanned, how often, and at what priority — including currency tiers.

---

## Freshness

Every read tool returns `as_of` (when the data was actually collected),
`age_minutes`, and `stale` (true past ~90 min). **Quote the age whenever it
matters** — never imply data is live when it isn't.

### `refresh_prices(league="")`

Fetches currency prices from poe.ninja immediately, bypassing the schedule.
Only worth calling when a result shows `stale: true`, or after the machine has
been asleep or offline — poe.ninja is CDN-cached ~30 min, so refreshing faster
returns identical data. Hits poe.ninja only; never touches GGG's rate budget.

## Prices

### `get_price(item_key, league="")` — **required:** `item_key`

Latest recorded price with spread and confidence. The return shape **differs by
source**, and conflating them mixes currencies.

**`source: "exchange"`** — two-sided book, everything in the base currency:

```json
{"source": "exchange", "price": 174.5, "price_currency": "exalted",
 "best_bid": 161.0, "best_ask": 188.0, "spread_pct": 15.5,
 "depth": 2140, "confidence": "medium: 15% spread"}
```

**`source: "search"`** — ask-side only, priced in whatever sellers used:

```json
{"source": "search", "price": 97695.0, "price_currency": "exalted",
 "listed_in": "divine", "listed_low": 220.0, "listed_high": 300.0,
 "confidence": "very-low: single listing"}
```

That item costs **260 divine** ≈ 97,695 exalted. Reporting "220–300 exalted"
would be wrong by ~375×.

> **Rule:** `price` is in `price_currency`. `listed_*` is in `listed_in`. Never
> compare across the two.

`found: false` means *not collected yet* — not that the item is unsellable.

### `get_price_history(item_key, league="", days=30, resolution="auto")`

OHLC candles. `resolution` is `auto` | `hourly` | `daily`; `auto` uses hourly
up to 14 days, daily beyond.

Returns `candles` plus a `summary` with `pct_change`. `samples` per candle
shows how well-supported it is — a candle built from one sample is a point, not
a range.

### `get_movers(league="", days=1, limit=20, min_samples=3)`

Largest percentage moves. Needs at least `days + 1` days of history to mean
anything; raise `min_samples` to suppress thinly-observed noise.


### `find_listings(name="", type="", league="", limit=10)` — *live*

Current listings with prices and **whisper strings**. Supply at least one of
`name` (unique) or `type` (base type).

Prefer `get_price` when recorded data will do.

### `prepare_trade(item_key="", name="", league="", max_price=0.0)` — *live*

Picks the best current listing and price-checks it against recorded history.
`max_price` filters in the listed currency; `0.0` means no cap.

Returns `chosen`, a `price_check` verdict, and a ready `whisper`.

> **Surface `price_check`**, especially when the live ask sits well above the
> recorded price.

---

## Arbitrage

### `find_arbitrage(league="", min_profit_pct=5.0, limit=20)`

Single currencies whose **median** bid exceeds their **median** ask.

Medians are deliberate: the cheapest ask is very often stale or mistyped.
Divine was observed with asks of 100/188/260/300 against bids of
230/200/180/162/160 — the extremes cross by 130 exalted and imply free money,
while the medians describe an ordinary spread.

Two lists come back:
- `opportunities` — crossings that survive at the median (genuine dislocations)
- `stale_extremes` — crossing only at the extremes; a **staleness signal, not
  an opportunity**

### `find_multi_step_arbitrage(league="", max_hops=4, min_profit_pct=1.0, max_age_minutes=120, limit=15)`

Profitable loops, e.g. `chaos → exalted → divine → chaos`. Pure local analysis
over collected direct pair rates — no API calls, free to re-run.

Returns `cycles` (with `gain_multiplier`, `profit_pct`, `min_depth`, per-leg
detail) and `direct_beats_routing` — the cheaper one-hop version of the same
idea, usually the more actionable.

`min_depth` bounds how much fits through the tightest leg.

**Requires direct pair rates.** Cross-rates synthesised through the base
currency can never show a profit — the gain factorises into per-currency
bid/ask ratios, each below 1. If this returns `ok: false`, the collector has
not built up `pair_rate` data yet (see [SETUP](SETUP.md)).

---

## Stash

Requires one-time setup — see [SETUP](SETUP.md#stash-access).

### `list_stash_items(league="", search="", tab="", rarity="", priced_only=False, group_stacks=True, sort="value", limit=200, offset=0)`

Full inventory **with quantities**.

- `group_stacks=True` (default) aggregates an item across tabs: three stacks of
  Exalted Orbs in two tabs become one row of 400. Usually the real question.
- `group_stacks=False` shows each physical stack and its tab.
- `rarity` — `Normal` | `Magic` | `Rare` | `Unique` | `Currency` | `Gem`
- `sort` — `value` | `quantity` | `name`
- Unpriced gear is **included** with a null value, so this is a real inventory
  rather than only the priced part.

### `get_stash_value(league="", refresh=False)`

Total value. `refresh=true` pulls a fresh stash read; otherwise the latest
stored snapshot is returned.

Reads **public listings** (no login), valuing currency at live poe.ninja
prices. Self-set gear prices are excluded by default as speculative. Sees
*listings*, not *holdings* — held/unlisted currency and Currency Exchange orders
are invisible. When the total looks low, say it reflects priced public listings,
not total wealth.

### `get_stash_history(league="", limit=30)`

Total value over time. Each snapshot is valued at the prices that applied then,
so the series separates "I acquired more" from "what I hold got dearer".

---

## Resources

Read these rather than guessing at behaviour:

| URI | Contents |
|---|---|
| `poe2market://guide` | Operating guide — read first |
| `poe2market://data-model` | Price semantics, junk filtering, candles |
| `poe2market://rate-limits` | Budgets, etiquette, compliance |
| `poe2market://state` | **Live**: active league, coverage, remaining budget |

---

## Mistakes that produce wrong answers

1. **Averaging a wide bid/ask** and calling it "the price". A 90% spread means
   there is no agreed price — quote both sides.
2. **Comparing `listed_low` (divine) to `price` (exalted).** A ~375× error.
3. **Reading empty history as evidence.** It means uncollected. Check
   `market_status`.
4. **Carrying a price between leagues.** Divine was 165 exalted in one league
   and 375 in another on the same day.
5. **Ignoring `confidence`.** `very-low` means one person's asking price.
6. **Looping `find_listings`.** It starves the collector, and that damage is
   permanent.
7. **Claiming a trade happened.** No API executes a PoE2 trade; tools return a
   whisper for a human to send.
