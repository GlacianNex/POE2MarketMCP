# poe2market — agent guide

Local, single-user Path of Exile 2 market data. Currency prices come from
**poe.ninja** (the in-game Currency Exchange); item and stash lookups come from
**GGG's trade API**.

## The one-paragraph version

A background collector polls poe.ninja on a schedule and writes to a local
SQLite database. Most tools read that database: free, instant, and bounded by
however long the collector has been running. A few tools call GGG's trade API
live (item/listing/stash lookups) and spend a shared rate budget — the same one
the player's own in-game trade uses, so use them sparingly. Every price is reported with a `confidence` field, because PoE2's
trade-site order book is thin and a midpoint can sit between a bid and an ask
that are 90% apart. **Never quote a price without checking `confidence`.**

## Choosing a tool

```
Need a number for something already tracked?   -> get_price
Need a trend or a chart?                       -> get_price_history (render it yourself)
Need what is on sale right now, or a whisper?  -> find_listings / prepare_trade
Need to know what exists / is tracked?         -> search_items / list_watchlists
Data looks missing or wrong?                   -> market_status  (read this before concluding anything)
```

`get_price` costs nothing. `find_listings` spends from a budget shared with the
collector. Prefer the former unless the user explicitly needs *right now*.

## Item keys

Every priceable thing has a stable key. Resolve user wording to a key with
`search_items` before calling anything else.

| Key shape | Meaning | Example |
|---|---|---|
| `cur:<exchange-id>` | A currency | `cur:divine`, `cur:chaos` |
| `<list>:<slug>` | A watchlist target | `uniq:mageblood`, `base:tri-res-amulet` |

Keys are not item names. `search_items(query="Mage")` → `uniq:mageblood`.

## Leagues

Omit the `league` argument and tools use the active challenge league, resolved
at runtime from the trade API's ordering. Pass an explicit name only when the
user asks about a specific league. `list_leagues` shows what exists and which
are being collected.

**Each league is a separate economy.** A Divine Orb was 165 exalted in one
league and 375 in another on the same day. Never carry a price across leagues.

## Reading a price correctly

`get_price` returns different shapes depending on where the data came from, and
mixing them up produces nonsense.

**`source: "exchange"`** — a two-sided book, all figures in the base currency:

```json
{ "source": "exchange", "price": 165.0, "price_currency": "exalted",
  "best_bid": 150.0, "best_ask": 180.0, "spread_pct": 18.2,
  "confidence": "medium: 18% spread" }
```

**`source: "search"`** — ask-side listings only, priced in whatever sellers
used. `listed_*` fields are in `listed_in`, **not** in the base currency:

```json
{ "source": "search", "price": 97695.0, "price_currency": "exalted",
  "listed_in": "divine", "listed_low": 220.0, "listed_high": 300.0,
  "confidence": "very-low: single listing" }
```

Here the item costs **260 divine**, which converts to ~97,695 exalted. Reporting
"220–300 exalted" would be wrong by a factor of ~375. Rule: **`price` is always
in `price_currency`; `listed_*` is always in `listed_in`.**

## Confidence, and when to refuse to quote

| Label | What to do |
|---|---|
| `high` | Quote it |
| `medium` | Quote it, mention the spread |
| `low` | Quote a range, not a point. Say the market is thin |
| `very-low: single listing` | One person's asking price, not a market price. Say so |

A 90% spread means there is no agreed price. Say "someone is asking X, someone
is bidding Y, there is no real market in between" rather than averaging them.

## Empty results are ambiguous

`found: false` or an empty `candles` array means **we have not collected it**,
not that the item is unsellable. Distinguish them:

1. Call `market_status` — if `raw_samples` is tiny or `earliest_sample` is
   recent, the collector simply has not run long enough.
2. Call `find_listings` for a live check.

History cannot be backfilled: GGG serves only current listings, so nothing
predates the collector's first run. Never imply a longer history than exists.

## Arbitrage

`find_arbitrage` screens single currencies where the median bid exceeds the
median ask. It deliberately uses medians: the cheapest ask is very often stale
or mistyped, and screening on extremes reports large opportunities in markets
that have none. Books crossing only at the extremes are reported separately
under `stale_extremes` — a staleness signal, not an opportunity.

`find_multi_step_arbitrage` finds loops (chaos → exalted → divine → chaos) over
stored direct pair rates. It makes no API calls, so it is free to re-run.
`min_depth` bounds how much fits through the tightest leg.

Both return **leads, not trades**. Every hop is a separate manual whisper
trade, so a four-hop loop needs four people to answer while prices hold. Say
this when presenting results.

## Trading

**No API executes a PoE2 trade.** `find_listings` and `prepare_trade` return a
`whisper` string; the trade completes manually in game via whisper → party →
trade window. Automating in-game input violates GGG's terms.

So: present the whisper, tell the user to send it, and stop. Never claim a
trade was placed, sent, or completed.

`prepare_trade` also price-checks the chosen listing against recorded history
and returns a `price_check` verdict — surface it, especially when the live ask
is well above the recorded price.

## Stash

`list_stash_items` and `get_stash_value` read the account's **public trade
listings** (no login), valuing currency at live poe.ninja prices. They see
*listings*, not *holdings*:

- Only items in a tab with a **buyout price** are visible. A merely-public,
  price-less tab is not indexed by GGG and shows nothing.
- Held currency never listed, and Currency Exchange orders, are invisible.
- By default, self-listed gear prices are excluded as speculative; only
  market-priced currency counts.

When a stash total looks low, say plainly that it reflects *priced public
listings*, not total wealth — the difference is usually unlisted currency.
## Things that will make you wrong

- Averaging a bid and an ask that are far apart and calling it "the price".
- Comparing `listed_low` (divine) against `price` (exalted).
- Treating an empty history as evidence about the market.
- Carrying a price from one league to another.
- Calling `find_listings` in a loop; it spends a budget shared with the
  collector, and starving the collector damages the history permanently.
