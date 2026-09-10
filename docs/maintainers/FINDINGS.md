# Findings

Everything here was measured against the live API, not assumed. Each entry
records what was expected, what was observed, and what changed as a result.
This is the most expensive knowledge in the project — the code can be
rewritten from it, but it cannot be re-derived without spending the requests
again.

Measurements were taken 2026-09-09/10 in *Runes of Aldur* and *Forbidden Rites*.

---

## 1. The trade API needs no authentication

**Expected:** a session cookie or OAuth token, as `api.pathofexile.com` requires.

**Observed:** `search`, `fetch`, `exchange` and all `data/*` endpoints return
200 unauthenticated. Only `api.pathofexile.com` (public stash river, profile,
stash) requires credentials — those return 401.

**Consequence:** market collection needs no credential at all. POESESSID is
required only for reading your own stash.

---

## 2. The exchange caps `want` at 10 ids

**Expected:** an arbitrarily long list of currencies per request.

**Observed:** 11 or more returns `{"error":{"code":2,"message":"Too many items
'want' items selected."}}`. Found by bisection: 10 succeeds, 11 fails.

**Consequence:** the single most structural constraint in the project. Pricing
all ~800 exchangeable currencies costs ~160 requests per pass against a budget
of 30 per 300s, so a flat "sweep everything" loop is impossible. Hence tiers:
core orbs every 10 minutes, the long tail every 6 hours, and an explicit fast
lane for the few currencies actually traded.

---

## 3. Direction is asymmetric — one-sided sampling under-reports

**Expected:** querying `have=exalted, want=X` returns the market in X.

**Observed:** it returns only people **selling** X (the ask). People **buying**
X appear solely under `have=X, want=exalted` (the bid). `chaos` showed *zero*
ask liquidity while carrying a live bid at 13.75 exalted — invisible to a
one-directional sweep, which would have recorded "no market".

**Consequence:** both directions are sampled, at double the request cost. The
recorded series is a two-sided book rather than a price.

---

## 4. Listings are stale — this is not a live order book

**Expected:** current listings.

**Observed:** measuring `indexed` against observation time across 59 stored
listings:

| Age when observed | |
|---|---|
| Median | **84.5 minutes** |
| Fresher than 10 min | 15 / 59 |
| Oldest | 16 days |

Eight consecutive samples of divine over 29 minutes produced **one** distinct
bid/ask state. The price did not move at all.

**Consequence:** polling faster than a couple of minutes buys nothing. The
binding constraint on freshness is GGG's indexing, not our rate budget. The
fast lane runs at 2 minutes; 1 minute was measured pushing the exchange budget
to 29/30 for no informational gain.

---

## 5. Junk listings are the majority, not the tail

**Expected:** occasional outliers, removable by a median or trimmed mean.

**Observed:** for divine in Forbidden Rites, 21 bid offers:

```
  genuine (8):  230, 200, 180, 162, 160, 160, 160, 140
  junk   (13):  1, 1, 1, 1, 1, 0.333, 0.1, 0.002, 0.001, 0.001, 0.0001 ...
```

Raw rows such as *"gives 1 exalted, takes 10000 divine"*. Junk **outnumbered**
signal, so the median of all 21 was `1.0`. Rank-based robust statistics fail
outright when the contamination is over half.

The ask side carried a different failure: a fat-fingered 100 against a market
around 190, which made the book appear crossed (best bid 230 > best ask 100)
and implied 130 exalted of free money.

**Consequence:** the three-step filter in [DATA_MODEL](../agent/DATA_MODEL.md) —
cluster, anchor on the real split, drop beyond 1σ. Junk is discarded before
measurement and never stored.

Depth was corrupted the same way: junk stock turned a real depth of a few
hundred into **19,469,825**.

---

## 6. Some pairs have no market at any price

**Expected:** any two currencies can be quoted against each other.

**Observed:** every mirror pair returned exactly `1.0` — `mirror -> alch`,
`mirror -> chaos`, `mirror -> divine`. A Mirror of Kalandra is worth on the
order of 100,000 Alchemy Orbs; nobody carries thousands of alchs to a trade
window, so no direct market exists and the only listings present are bait
reading "1 of mine for 1 of yours".

**Consequence:** a value-ratio gate (`PAIR_MAX_VALUE_RATIO = 200`). Such pairs
are never queried, which removes the junk at its source and stops spending
requests to rediscover dead markets. Confirmed against measured liquidity:
`mirror` had **zero** two-sided samples.

---

## 7. Only a handful of currencies actually trade on both sides

**Observed:** of 215 currencies with a collected price in Forbidden Rites,
**6** had two-sided liquidity: divine, chaos, annul, alch, waystone-1,
waystone-3. Everything else was ask-only, typically on 1–2 offers.

**Consequence:** `poe2market suggest-pairs` derives the pair set from measured
liquidity instead of guesswork. An ask-only currency cannot be sold at its
quoted price and cannot close a trade loop.

---

## 8. Synthetic cross-rates can never yield arbitrage

**Expected:** cross-rates computed locally from base-relative prices might
reveal triangular arbitrage for free.

**Observed:** zero profitable cycles, always. The gain of any cycle
factorises:

```
(bid_A/ask_B)(bid_B/ask_C)(bid_C/ask_A) = (bid_A/ask_A)(bid_B/ask_B)(bid_C/ask_C)
```

Every term is below 1 whenever a spread exists. With four realistic books the
product was 0.5850 — a 4-hop loop returning **−41.5%**.

**Consequence:** this is arithmetic, not a data problem, and no amount of local
computation escapes it. Genuine multi-hop arbitrage requires **direct** pair
quotes, which is the entire justification for the `pair_rate` sweep.

---

## 9. PoE2 leagues carry no date metadata

**Expected:** league start dates, to identify "the current league".

**Observed:** `pathofexile.com/api/leagues` serves **PoE1 only** — it ignores
`realm=poe2` and returns leagues with `realm: pc` dating to 2013.
`api.pathofexile.com/league` requires OAuth. `trade2/data/leagues` gives ids
and ordering, nothing more.

**Consequence:** "current" is defined as the first entry that is neither
permanent nor a variant, relying on GGG listing the active challenge league
first. Confirmed correct against the live list (`Forbidden Rites`). The
resolution is cached so an API failure cannot silently redirect collection.

---

## 10. The in-game Currency Exchange has no API

**Observed:** no order-book endpoint exists. `trade2/market`,
`trade2/orderbook`, `currency-exchange` and the documented poe.ninja PoE2
endpoint all return 404. poe.ninja's own page loads its data through a
client-side call not visible in the HTML or its bundle.

**Consequence:** `trade2` covers the **trade site** — players' listed stash
tabs — not the in-game Currency Exchange where most volume flows with far
narrower spreads. This explains why 209 of 215 currencies show one-sided
markets on 1–2 offers. It is the slice you can actually buy from by whisper,
which makes it the right slice for trade preparation, but it is not the whole
economy and should never be described as such.

---

## 11. Bugs found by reading output, not by testing

Recorded because each was invisible to unit tests and only surfaced from
looking at real numbers:

| Symptom | Cause |
|---|---|
| Mageblood: `bid/ask 220/300` against `mid 97,695` | `low`/`high` mean bid/ask for exchange rows but percentiles in the *listed* currency for search rows. A ~375x unit error |
| Divine sampled twice, 16s apart | Tier de-duplication ignored explicit `currencies` lists, so the fast lane was re-swept by `core` |
| `rate_budget: []` on a fresh process | Learned rate-limit rules were per-process; now persisted |
| Label read `divine`, not `Divine Orb` | The sweep's upsert overwrote the catalogued display name with the raw id |
| `pair_currencies` silently ignored | TOML: top-level keys appended *after* `[[table]]` sections get parsed into the last table |
| `divine -> annul = 1.0` stored as real | Pair rates with no base-price anchor passed unfiltered; now dropped entirely |

---

## 12. The trade2 `exchange` endpoint reads a near-dead market

**Expected:** the trade API's currency exchange reflects PoE2 currency prices.

**Observed:** a liquid currency showed **5 offers** on `trade2/exchange`
(`total=5` for chaos, `total=1` for selling transmute). poe.ninja reported
**73,724** divine trades over the same period. PoE2 currency trades almost
entirely through the **in-game Currency Exchange**, which players use instead
of the old bulk-listing system the trade API exposes.

Prices were consequently wrong and unfixable by filtering — transmute stored at
155 ex when its real value is ~0.5 ex, because 2 junk asks *are* the whole
cluster.

**Consequence:** currency is priced from **poe.ninja** instead
(`/poe2/api/economy/exchange/current/overview`), which surfaces in-game
Currency Exchange data. See [POE_NINJA_CATEGORIES](POE_NINJA_CATEGORIES.md).
trade2 is kept only for item/unique search, where it returns hundreds of real
listings. `currency_rate` prefers `source='ninja'` samples over `'exchange'`.

Verified: divine 266 ex, chaos 21.9 ex, transmute 0.52 ex — all match
poe.ninja and a real in-game trade.

---

## 13. poe.ninja cache and refresh timing

**Observed** from live response headers:

```
cache-control: public, max-age=1800, stale-while-revalidate=300, stale-if-error=86400
age: 1350
```

- CDN-cached **30 minutes** (`max-age=1800`), not the 5 min the docs imply.
- Served stale for 5 more min while revalidating.
- Underlying aggregation refreshes **~hourly**.

**Consequence:** `ninja_cadence_minutes` defaults to 30 — polling faster just
returns the identical cached body. Currency prices are inherently up to ~1h
stale; fine for valuation, verify live before a trade.

---

## 14. Stash = public listings, and how GGG indexes them

The personal stash API is OAuth-only (closed) and the POESESSID cookie is
403-forbidden for PoE2 stash. The working path is the **public trade search
filtered by account name** — no auth. Findings from debugging why a player's
items were missing:

- **`status: any` is required.** `status: online` returns 0 when the account
  owner is offline; `any` catches offline listings. The whole stash reads as
  offline whenever the player is not in game.
- **Trade search caps at 100 result ids**, sorted price-ascending — a single
  query returns only the cheapest 100 of a larger stash. Fetch **per item
  category** (`currency`, `gem`, `weapon`, …) to stay under the cap and get
  everything.
- **An item indexes only when its tab has a buyout price.** Observed: every
  indexed tab was named `~price …` or `~b/o …`; items inherit the tab price and
  index even when not individually priced. A tab set merely "public" without a
  price does **not** feed trade search. This is why a player's Whittling omens
  in a price-less tab were invisible while items in `~price 99 waystone-N` tabs
  showed.
- **Only public listings are ever visible.** A currency stockpile held but not
  listed, and anything in the in-game Currency Exchange, cannot be seen at all.

**Consequence:** the stash tools read *listings*, not *holdings*. `search_by_
account` splits by category; valuation prices currency from poe.ninja and (off
by default) can use the seller's own listed price for gear.

---

## 15. The account search silently dropped half the stash (our bug)

**Observed:** the trade site showed 251 items for an account; our identical
account search returned 122. The user's own screenshot had **Sale Type: Any**
set. Our query omitted `sale_type`, so the API applied its default (priced
listings only), silently excluding everything without a buyout price.

**Fix:** send `filters.trade_filters.filters.sale_type: {option: "any"}`. Count
went 122 → 251, matching the site exactly.

**Lesson (the expensive one):** when our output disagreed with the user's
ground truth, the bug was in OUR query the whole time — not GGG's index. Hours
were lost inventing theories about GGG ("special tabs don't index", "not
crawled", "wrong league") instead of auditing our own request against the docs
and the user's visible filter settings. **Suspect our own new code before a
system serving thousands of players.** See the global rules in
`~/.claude/CLAUDE.md`.

---

## 16. A stale resolved-league cache valued the wrong league

**Observed:** after testing a different league (Runes of Aldur) and reverting
`config.toml` to Forbidden Rites, `stash` still valued Runes of Aldur. The
resolved league is cached in the DB `meta` table (`resolved_leagues`); editing
config does not clear it, and `_league()` prefers the cache.

**Fix:** the cache must be updated/cleared when the configured league changes.
Interim: `Store.set_meta("resolved_leagues", <league>)`. Note for future work —
a config change to `leagues` should invalidate the cache automatically.

---

## 17. GGG trade indexing needs the account ONLINE

**Observed:** a stash change made while offline never reached the index. The
newest `indexed` timestamp sat frozen for ~3 hours despite tab edits. The moment
the user logged in AND a content crawl ran (timestamp jumped), the new items
appeared (total 122 → 123).

**Consequence:** `status: online` returns 0 for an offline owner — always query
`status: any`. And the data is only as fresh as GGG's last *content* crawl of
the account, which is gated on the owner being online. When a stash total looks
wrong, check the newest `indexed` timestamp before theorising about anything
else.
