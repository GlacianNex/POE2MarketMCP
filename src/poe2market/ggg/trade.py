"""Typed wrappers over the trade2 endpoints, plus query construction.

Endpoint budgets differ by an order of magnitude, which drives how the
collector spends them:

===============  ==================================  ==========================
Endpoint         Limit (per IP, unauthenticated)     Cost per priced item
===============  ==================================  ==========================
``exchange``     30 / 300s                           ~1/5th of a request
``search``       600 / 21600s                        1 request
``fetch``        1000 / 21600s (10 listings each)    1 request per 10 listings
===============  ==================================  ==========================

Both ``have`` and ``want`` are arrays capped at 10 entries, so one request
prices up to 10 currencies. Named items have no such batching: each costs a
search plus a fetch, which is why they are scheduled from watchlists rather
than swept.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from ..config import WatchTarget
from .client import TRADE_BASE, GGGClient, league_path

log = logging.getLogger(__name__)

# Hard server cap: 11+ entries returns 'Too many items `want` items selected'.
EXCHANGE_BATCH = 10
FETCH_BATCH = 10

# Width of the price cluster treated as "the same market", in log10 units.
# 0.5 spans roughly a 3x range: wider than any real bid/ask spread, far
# narrower than the gap to junk listings.
CLUSTER_WINDOW_LOG10 = 0.5

#: Deviations from the real split beyond which a listing is discarded.
#: 1.0 keeps only the tight core around where the two sides actually meet.
#: Measured on live divine data, 1.0 and 1.5 both yield a 15.5% spread while
#: 2.0 admits stale asks and doubles it to 32.7%.
OUTLIER_SIGMAS = 1.0

#: Turns a median-absolute-deviation into a standard-deviation equivalent.
MAD_TO_SIGMA = 1.4826


@dataclass
class RawOffer:
    """One exchange offer, kept in the API's own terms.

    The endpoint is written from the *lister's* perspective: they hand over
    ``gives`` and want ``takes`` back. Which of those is the priced item
    depends on the query direction, so no ratio is computed here.
    """

    listing_hash: str
    gives_currency: str
    gives_amount: float
    takes_currency: str
    takes_amount: float
    stock: int
    account: str | None = None
    whisper: str | None = None
    indexed_at: str | None = None


@dataclass
class Offer:
    """An offer resolved to 'price of one unit, denominated in the base'."""

    listing_hash: str
    currency: str
    base: str
    price: float
    stock: int
    account: str | None = None
    whisper: str | None = None
    indexed_at: str | None = None


@dataclass
class MarketBook:
    """Two-sided view of one currency, denominated in the base currency.

    Both the extremes and the medians are kept, because they answer different
    questions and the extremes are unreliable on their own.

    Listings here are asynchronous standing offers — the median one is ~85
    minutes old when GGG serves it — so the cheapest ask is frequently a seller
    who has already gone. Observed live: divine asks of 100/188/260/300 against
    bids of 230/200/180/162/160. The extremes cross (bid 230 > ask 100) and
    imply free money; the medians (ask 224, bid 180) describe a normal 22%
    spread. So the medians drive the recorded price series, and the extremes
    are reported alongside for anyone actually placing a trade.
    """

    currency: str
    base: str
    best_ask: float | None = None     # cheapest listed; try this first
    best_bid: float | None = None     # highest bid listed
    median_ask: float | None = None   # what the market really costs
    median_bid: float | None = None
    anchor: float | None = None   # midpoint of the closest ask/bid pair
    ask_depth: int = 0
    bid_depth: int = 0
    n_asks_raw: int = 0      # before junk was filtered
    n_bids_raw: int = 0
    asks: list[Offer] = field(default_factory=list)
    bids: list[Offer] = field(default_factory=list)

    @property
    def mid(self) -> float | None:
        """Robust mid price. Falls back to whichever side exists."""
        if self.median_ask is not None and self.median_bid is not None:
            return (self.median_ask + self.median_bid) / 2
        if self.median_ask is not None:
            return self.median_ask
        return self.median_bid

    @property
    def spread_pct(self) -> float | None:
        """Round-trip cost as a percentage, from the medians."""
        if self.median_ask and self.median_bid and self.mid:
            return (self.median_ask - self.median_bid) / self.mid * 100.0
        return None

    @property
    def is_crossed(self) -> bool:
        """True when the best bid exceeds the best ask.

        Usually an artefact of stale listings rather than a live opportunity.
        """
        return bool(
            self.best_bid and self.best_ask and self.best_bid > self.best_ask
        )

    @classmethod
    def build(
        cls, currency: str, base: str, asks: list[Offer], bids: list[Offer],
        *, sigmas: float = OUTLIER_SIGMAS,
    ) -> "MarketBook":
        # Junk is discarded before anything is measured, including depth:
        # counting stock behind a nonsense price overstates the market badly.
        ask_prices, bid_prices, anchor = resolve_market(
            [o.price for o in asks], [o.price for o in bids], sigmas=sigmas
        )
        ask_set, bid_set = set(ask_prices), set(bid_prices)
        real_asks = [o for o in asks if o.price in ask_set]
        real_bids = [o for o in bids if o.price in bid_set]

        return cls(
            currency=currency,
            base=base,
            best_ask=ask_prices[0] if ask_prices else None,
            best_bid=bid_prices[-1] if bid_prices else None,
            median_ask=statistics.median(ask_prices) if ask_prices else None,
            median_bid=statistics.median(bid_prices) if bid_prices else None,
            ask_depth=sum(o.stock for o in real_asks),
            bid_depth=sum(o.stock for o in real_bids),
            anchor=anchor,
            n_asks_raw=len(asks),
            n_bids_raw=len(bids),
            asks=sorted(real_asks, key=lambda o: o.price),
            bids=sorted(real_bids, key=lambda o: -o.price),
        )


@dataclass
class PricePoint:
    """Aggregated price statistics for one item at one moment."""

    n_listings: int
    currency: str
    low: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    high: float | None = None
    total_stock: int = 0
    listings: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_prices(
        cls,
        prices: list[float],
        currency: str,
        *,
        total_stock: int = 0,
        listings: list[dict[str, Any]] | None = None,
    ) -> "PricePoint":
        if not prices:
            return cls(n_listings=0, currency=currency, listings=listings or [])
        ordered = sorted(prices)
        return cls(
            n_listings=len(ordered),
            currency=currency,
            low=ordered[0],
            p25=_quantile(ordered, 0.25),
            median=statistics.median(ordered),
            p75=_quantile(ordered, 0.75),
            high=ordered[-1],
            total_stock=total_stock,
            listings=listings or [],
        )


def _quantile(ordered: list[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def dominant_cluster(prices: list[float], window: float = CLUSTER_WINDOW_LOG10) -> list[float]:
    """Return the largest tightly-grouped run of prices, discarding junk.

    The exchange carries listings that are real rows but meaningless prices —
    observed live for divine: eight genuine bids of 140-230 exalted alongside
    thirteen entries like "gives 1 exalted, takes 10000 divine". Junk was the
    *majority*, so a median or a trimmed mean lands squarely in it.

    What separates them is not rank but spread: genuine listings for one item
    cluster inside a factor of ~2, while junk scatters across four orders of
    magnitude. So work in log space and take the biggest cluster that fits in
    ``window``, which survives junk outnumbering signal.

    Ties go to the more expensive cluster: for a currency, the junk is
    overwhelmingly lowball, and picking the higher group is the safer error.
    """
    positive = sorted(p for p in prices if p and p > 0)
    if len(positive) <= 2:
        return positive

    logs = [math.log10(p) for p in positive]
    best_start, best_len = 0, 0
    end = 0
    for start in range(len(logs)):
        if end < start:
            end = start
        while end + 1 < len(logs) and logs[end + 1] - logs[start] <= window:
            end += 1
        size = end - start + 1
        # >= keeps the later (more expensive) cluster on a tie.
        if size >= best_len:
            best_start, best_len = start, size
    return positive[best_start : best_start + best_len]


def _median(xs: list[float]) -> float:
    return statistics.median(xs)


def resolve_market(
    ask_prices: list[float],
    bid_prices: list[float],
    *,
    sigmas: float = OUTLIER_SIGMAS,
) -> tuple[list[float], list[float], float | None]:
    """Find the real split, then drop everything too far from it.

    The true market sits where the two sides very nearly meet: the closest
    ask/bid pair. Listings far from that point are stale, mistyped, or junk —
    on this endpoint the junk regularly *outnumbers* the genuine listings, so
    the outlier test has to be anchored on something the junk cannot move.

    The procedure:

    1. Take the largest tight cluster across both sides combined. This locates
       the market even when junk is the majority, because genuine listings for
       one item group inside a factor of a few while junk sprays across orders
       of magnitude.
    2. Inside that cluster, find the closest ask/bid pair — the real split.
       Its midpoint is the anchor.
    3. Measure spread in log space with a median-absolute-deviation, which a
       handful of survivors cannot skew, and drop anything beyond ``sigmas``.

    Returns ``(kept_asks, kept_bids, anchor)``.
    """
    combined = [p for p in ask_prices + bid_prices if p and p > 0]
    if not combined:
        return [], [], None

    # 1. Where is the market at all?
    core = set(dominant_cluster(combined))
    asks_in = sorted(p for p in ask_prices if p in core)
    bids_in = sorted(p for p in bid_prices if p in core)

    # 2. The real split: the ask and bid that come closest to meeting.
    anchor: float | None
    if asks_in and bids_in:
        best_pair = min(
            ((a, b) for a in asks_in for b in bids_in),
            key=lambda ab: abs(ab[0] - ab[1]),
        )
        anchor = (best_pair[0] + best_pair[1]) / 2.0
    else:
        present = asks_in or bids_in
        anchor = _median(present) if present else None
    if anchor is None or anchor <= 0:
        return asks_in, bids_in, anchor

    # 3. Robust dispersion about the anchor, in log space so the test is
    #    multiplicative — "twice the price" is one kind of error regardless of
    #    whether the item costs 1 exalted or 1000.
    inside = asks_in + bids_in
    log_anchor = math.log10(anchor)
    deviations = [abs(math.log10(p) - log_anchor) for p in inside]
    mad = _median(deviations) if deviations else 0.0
    sigma = mad * MAD_TO_SIGMA

    if sigma <= 0:
        # Every survivor sits on the anchor; allow a small floor so a lone
        # legitimate tick either side is not thrown away.
        sigma = 0.05

    limit = sigmas * sigma
    keep = lambda p: abs(math.log10(p) - log_anchor) <= limit
    return [p for p in asks_in if keep(p)], [p for p in bids_in if keep(p)], anchor


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class TradeAPI:
    def __init__(self, client: GGGClient) -> None:
        self.client = client

    # -- reference data -------------------------------------------------

    async def leagues(self) -> list[dict[str, Any]]:
        data = await self.client.request(
            "GET", f"{TRADE_BASE}/data/leagues", "trade-data"
        )
        return data.get("result", [])

    async def static_data(self) -> list[dict[str, Any]]:
        data = await self.client.request(
            "GET", f"{TRADE_BASE}/data/static", "trade-data"
        )
        return data.get("result", [])

    async def item_data(self) -> list[dict[str, Any]]:
        data = await self.client.request(
            "GET", f"{TRADE_BASE}/data/items", "trade-data"
        )
        return data.get("result", [])

    async def stat_data(self) -> list[dict[str, Any]]:
        data = await self.client.request(
            "GET", f"{TRADE_BASE}/data/stats", "trade-data"
        )
        return data.get("result", [])

    # -- bulk currency --------------------------------------------------

    async def exchange_raw(
        self, league: str, have: list[str], want: list[str], *, min_stock: int = 1
    ) -> list[RawOffer]:
        """One exchange call, returned as unresolved offers.

        Both arrays are capped at :data:`EXCHANGE_BATCH` by the server.
        """
        body = {
            "query": {
                "status": {"option": "online"},
                "have": have[:EXCHANGE_BATCH],
                "want": want[:EXCHANGE_BATCH],
                "minimum": min_stock,
            },
            "sort": {"have": "asc"},
            "engine": "new",
        }
        data = await self.client.request(
            "POST",
            f"{TRADE_BASE}/exchange/{league_path(league)}",
            "trade-exchange",
            json_body=body,
        )

        offers: list[RawOffer] = []
        for listing_hash, entry in (data.get("result") or {}).items():
            listing = entry.get("listing") or {}
            account = (listing.get("account") or {}).get("name")
            for offer in listing.get("offers") or []:
                gives = offer.get("item") or {}
                takes = offer.get("exchange") or {}
                g_amt, t_amt = gives.get("amount") or 0, takes.get("amount") or 0
                g_cur, t_cur = gives.get("currency"), takes.get("currency")
                if not (g_amt and t_amt and g_cur and t_cur):
                    continue
                offers.append(
                    RawOffer(
                        listing_hash=listing_hash,
                        gives_currency=g_cur,
                        gives_amount=float(g_amt),
                        takes_currency=t_cur,
                        takes_amount=float(t_amt),
                        stock=int(gives.get("stock") or 0),
                        account=account,
                        whisper=listing.get("whisper"),
                        indexed_at=listing.get("indexed"),
                    )
                )
        return offers

    async def exchange_book(
        self, league: str, base: str, currencies: list[str],
        *, sigmas: float = OUTLIER_SIGMAS,
    ) -> dict[str, MarketBook]:
        """Build a bid/ask book for each currency, priced in ``base``.

        Direction is not symmetric on this endpoint, and both halves are
        needed:

        * ``have=base, want=[C...]`` — listers hand over C and take base.
          That is the **ask**: what it costs us to buy one C.
        * ``have=[C...], want=base`` — listers hand over base and take C.
          That is the **bid**: what we receive selling one C.

        Sampling only one direction systematically under-reports illiquid
        currencies. Cost is two request batches per 10 currencies.
        """
        asks: dict[str, list[Offer]] = {}
        bids: dict[str, list[Offer]] = {}

        for batch in _chunks(currencies, EXCHANGE_BATCH):
            # Ask side: the priced item is what the lister gives.
            try:
                for o in await self.exchange_raw(league, [base], batch):
                    if o.takes_currency != base:
                        continue
                    asks.setdefault(o.gives_currency, []).append(
                        Offer(
                            listing_hash=o.listing_hash,
                            currency=o.gives_currency,
                            base=base,
                            price=o.takes_amount / o.gives_amount,
                            stock=o.stock,
                            account=o.account,
                            whisper=o.whisper,
                            indexed_at=o.indexed_at,
                        )
                    )
            except Exception as exc:  # one bad batch must not sink the sweep
                log.warning("ask batch %s failed: %s", batch[:3], exc)

            # Bid side: the priced item is what the lister takes.
            try:
                for o in await self.exchange_raw(league, batch, [base]):
                    if o.gives_currency != base:
                        continue
                    price = o.gives_amount / o.takes_amount
                    # o.stock counts the base currency the buyer is holding.
                    # Restate it as 'units of C they can absorb' so bid and ask
                    # depth are in the same unit and comparable.
                    absorb = int(o.stock / price) if price else 0
                    bids.setdefault(o.takes_currency, []).append(
                        Offer(
                            listing_hash=o.listing_hash,
                            currency=o.takes_currency,
                            base=base,
                            price=price,
                            stock=absorb,
                            account=o.account,
                            whisper=o.whisper,
                            indexed_at=o.indexed_at,
                        )
                    )
            except Exception as exc:
                log.warning("bid batch %s failed: %s", batch[:3], exc)

        return {
            c: MarketBook.build(
                c, base, asks.get(c) or [], bids.get(c) or [], sigmas=sigmas
            )
            for c in currencies
            if asks.get(c) or bids.get(c)
        }

    # -- named item search ----------------------------------------------

    async def search(
        self, league: str, query: dict[str, Any]
    ) -> tuple[str, list[str], int]:
        """Run a search; return (query_id, listing ids, total matches)."""
        data = await self.client.request(
            "POST",
            f"{TRADE_BASE}/search/{league_path(league)}",
            "trade-search",
            json_body=query,
        )
        return data.get("id", ""), data.get("result", []) or [], data.get("total", 0)

    async def fetch(self, ids: list[str], query_id: str) -> list[dict[str, Any]]:
        """Hydrate up to ``FETCH_BATCH`` listing ids into full listings."""
        if not ids:
            return []
        data = await self.client.request(
            "GET",
            f"{TRADE_BASE}/fetch/{','.join(ids[:FETCH_BATCH])}",
            "trade-fetch",
            params={"query": query_id},
        )
        return [r for r in (data.get("result") or []) if r]

    async def search_by_account(
        self, league: str, account: str, *, max_items: int = 300
    ) -> list[dict[str, Any]]:
        """Every item an account has **publicly listed**, with prices.

        The working path to "my stash" on PoE2: the personal stash API needs
        OAuth (closed to new apps) and the POESESSID cookie is 403-forbidden
        for stash, but the public trade search filters by account name with no
        auth at all. The tradeoff is inherent — it sees only tabs the owner
        marked public/indexed, not private ones.

        The full account name including the "#1234" discriminator is required
        here — the trade filter matches the exact handle (unlike the legacy
        stash endpoint, which rejects the discriminator).
        """
        # trade2 search caps at 100 result ids and sorts price-ascending, so a
        # single query returns only the cheapest 100 of a larger stash — the
        # valuable items get cut. Splitting by item category keeps each subquery
        # under the cap, so the whole stash is retrievable.
        categories = [
            "currency", "gem", "weapon", "armour", "accessory",
            "jewel", "flask", "map", "sanctum",
        ]

        def account_query(category: str | None) -> dict[str, Any]:
            # sale_type "any" and no listed constraint match the trade site's
            # "Sale Type: Any / Listed: Any Time". Without sale_type the API
            # defaults to priced listings only, silently dropping items — the
            # cause of an account showing 122 here vs 251 on the site.
            filters: dict[str, Any] = {
                "trade_filters": {
                    "filters": {
                        "account": {"input": account},
                        "sale_type": {"option": "any"},
                    }
                }
            }
            if category:
                filters["type_filters"] = {
                    "filters": {"category": {"option": category}}
                }
            return {
                "query": {"status": {"option": "any"}, "filters": filters},
                "sort": {"price": "asc"},
            }

        seen: set[str] = set()
        ids_by_query: list[tuple[str, list[str]]] = []
        for cat in categories + [None]:
            try:
                qid, ids, _ = await self.search(league, account_query(cat))
            except Exception as exc:
                log.debug("account category %s failed: %s", cat, exc)
                continue
            fresh = [i for i in ids if i not in seen]
            seen.update(fresh)
            if fresh:
                ids_by_query.append((qid, fresh))

        if not ids_by_query:
            return []

        rows: list[dict[str, Any]] = []
        fetched = 0
        for qid, ids in ids_by_query:
            for chunk in _chunks(ids, FETCH_BATCH):
                if fetched >= max_items:
                    break
                rows.extend(await self.fetch(chunk, qid))
                fetched += len(chunk)

        out: list[dict[str, Any]] = []
        for row in rows:
            listing = row.get("listing") or {}
            price = listing.get("price") or {}
            item = row.get("item") or {}
            out.append(
                {
                    "listing_hash": row.get("id"),
                    "name": (item.get("name") or "").strip() or None,
                    "type_line": item.get("typeLine") or item.get("baseType"),
                    "stack_size": int(item.get("stackSize") or 1),
                    "rarity": {0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique",
                               4: "Gem", 5: "Currency", 6: "Divination Card"}.get(
                        item.get("frameType")),
                    "ilvl": item.get("ilvl") or item.get("itemLevel"),
                    "price_amount": price.get("amount"),
                    "price_currency": price.get("currency"),
                    "tab_name": (listing.get("stash") or {}).get("name"),
                    "whisper": listing.get("whisper"),
                    "raw": item,
                }
            )
        return out

    async def price_target(self, league: str, target: WatchTarget) -> PricePoint:
        """Search + fetch one watch target and aggregate its listings."""
        query = build_query(target)
        query_id, ids, _total = await self.search(league, query)
        if not ids:
            return PricePoint(n_listings=0, currency="")

        wanted = min(target.sample_size, len(ids))
        rows: list[dict[str, Any]] = []
        for chunk in _chunks(ids[:wanted], FETCH_BATCH):
            rows.extend(await self.fetch(chunk, query_id))

        # Listings can be priced in different currencies. Group by currency and
        # keep the largest group, so percentiles are computed over comparable
        # numbers; the rest are still stored as individual listings.
        by_currency: dict[str, list[float]] = {}
        listings: list[dict[str, Any]] = []
        for row in rows:
            listing = row.get("listing") or {}
            price = listing.get("price") or {}
            amount, cur = price.get("amount"), price.get("currency")
            if amount is None or not cur:
                continue
            by_currency.setdefault(cur, []).append(float(amount))
            account = listing.get("account") or {}
            listings.append(
                {
                    "listing_hash": row.get("id"),
                    "account": account.get("name"),
                    "character_name": account.get("lastCharacterName"),
                    "is_online": bool(account.get("online")),
                    "price_amount": float(amount),
                    "price_currency": cur,
                    "stock": None,
                    "indexed_at": listing.get("indexed"),
                    "whisper": listing.get("whisper"),
                    "item": row.get("item") or {},
                }
            )

        if not by_currency:
            return PricePoint(n_listings=0, currency="", listings=listings)
        currency, prices = max(by_currency.items(), key=lambda kv: len(kv[1]))

        # Item listings carry the same junk as the currency exchange: bait
        # prices (a chase unique at 1 exalted to farm whispers) and stale
        # leftovers. Keep only the dominant cluster, and drop the rejected
        # listings entirely rather than storing rows that would have to be
        # filtered again on every read.
        kept = set(dominant_cluster(prices))
        if kept:
            prices = [p for p in prices if p in kept]
            listings = [
                L for L in listings
                if L.get("price_currency") != currency
                or L.get("price_amount") in kept
            ]
        return PricePoint.from_prices(prices, currency, listings=listings)


def build_query(target: WatchTarget) -> dict[str, Any]:
    """Turn a :class:`WatchTarget` into a trade2 search body."""
    if target.raw_query:
        query = dict(target.raw_query)
        query.setdefault("sort", {"price": "asc"})
        return query

    inner: dict[str, Any] = {
        "status": {"option": "online"},
        "stats": [{"type": "and", "filters": []}],
    }
    if target.name:
        inner["name"] = target.name
    if target.type:
        inner["type"] = target.type
    if target.filters:
        inner["filters"] = target.filters

    return {"query": inner, "sort": {"price": "asc"}}
