"""Collection jobs: the units of work the daemon schedules.

Each job is idempotent and self-contained, so a crash mid-sweep costs at most
one pass and a restart needs no recovery logic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..config import Config, CurrencyTier, Watchlist
from ..ggg.trade import TradeAPI
from ..store.db import Store, utcnow

#: How far a direct pair rate may stray from the implied cross-rate
#: before it is treated as junk. Wide on purpose: a real
#: dislocation is the signal, a 100x discrepancy is noise.
PAIR_PLAUSIBILITY = 4.0

#: Largest value ratio worth quoting directly. A Mirror is worth roughly
#: 100,000 Alchemy Orbs, so no real mirror<->alch market exists: nobody hauls
#: thousands of alchs to a trade window. The listings that do appear on such
#: pairs are bait, and observably all read "1 of mine for 1 of yours" — which
#: is why every mirror pair came back at exactly 1.0. Skipping them removes
#: the junk at its source and stops spending requests on dead pairs.
PAIR_MAX_VALUE_RATIO = 200.0

log = logging.getLogger(__name__)

# Static-data categories that are not actually tradeable on the exchange.
SKIP_CATEGORIES = {"Misc"}


class Collector:
    def __init__(self, cfg: Config, api: TradeAPI, store: Store) -> None:
        self.cfg = cfg
        self.api = api
        self.store = store
        self._taxonomy: dict[str, list[dict[str, Any]]] | None = None

    # -- taxonomy -------------------------------------------------------

    async def taxonomy(self, refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
        """Currency ids grouped by static-data category, cached per process."""
        if self._taxonomy is not None and not refresh:
            return self._taxonomy
        groups = await self.api.static_data()
        out: dict[str, list[dict[str, Any]]] = {}
        for g in groups:
            gid = g.get("id") or ""
            if gid in SKIP_CATEGORIES:
                continue
            entries = [
                e for e in (g.get("entries") or [])
                # Separators come through as blank-text entries with a shared id.
                if e.get("id") and e.get("text")
            ]
            if entries:
                out[gid] = entries
        self._taxonomy = out
        return out

    async def tier_currencies(self, tier: CurrencyTier) -> list[str]:
        """Resolve a tier's categories to a concrete list of exchange ids."""
        tax = await self.taxonomy()
        claimed: set[str] = set()
        for other in self.cfg.currency_tiers:
            if other.name == tier.name:
                break
            # Explicit currency lists claim their ids too. Without this a
            # fast-lane orb is also swept by the broad tier below it, paying
            # twice for the same price.
            claimed.update(other.currencies)
            for cat in other.categories:
                if cat != "*":
                    claimed.update(e["id"] for e in tax.get(cat, []))

        # An explicit list wins outright: it is a deliberate fast lane, so it
        # is not filtered against what earlier tiers already claimed.
        if tier.currencies:
            return list(dict.fromkeys(tier.currencies))

        ids: list[str] = []
        if "*" in tier.categories:
            for cat, entries in tax.items():
                ids.extend(e["id"] for e in entries)
        else:
            for cat in tier.categories:
                ids.extend(e["id"] for e in tax.get(cat, []))

        seen: set[str] = set()
        ordered = []
        for i in ids:
            if i in claimed or i in seen:
                continue
            seen.add(i)
            ordered.append(i)
        return ordered

    async def register_currency_items(self) -> None:
        """Make sure every exchangeable currency has an ``item`` row."""
        tax = await self.taxonomy()
        for category, entries in tax.items():
            for e in entries:
                self.store.upsert_item(
                    f"cur:{e['id']}",
                    "currency",
                    e.get("text") or e["id"],
                    currency_id=e["id"],
                    category=category,
                    icon=e.get("image"),
                )

    # -- jobs -----------------------------------------------------------

    async def sweep_currency(self, league: str, tier: CurrencyTier) -> dict[str, Any]:
        """Price one tier's currencies against the base and store a sample."""
        started = utcnow()
        base = self.cfg.base_currency
        ids = [c for c in await self.tier_currencies(tier) if c != base]
        if not ids:
            return {"tier": tier.name, "priced": 0, "skipped": "no currencies"}

        await self.register_currency_items()
        league_id = self.store.league_id(league, self.cfg.realm)
        ts = utcnow()
        priced = 0

        try:
            book = await self.api.exchange_book(
                league, base, ids, sigmas=self.cfg.outlier_sigmas
            )
        except Exception as exc:
            log.exception("currency sweep failed for tier %s", tier.name)
            self.store.log_run(
                f"currency:{tier.name}", league, started, "error", detail=str(exc)
            )
            return {"tier": tier.name, "priced": 0, "error": str(exc)}

        tax = await self.taxonomy()
        display = {
            e["id"]: e.get("text") or e["id"]
            for entries in tax.values() for e in entries
        }

        for currency_id, mb in book.items():
            mid = mb.mid
            if mid is None:
                continue
            # Reject unreliable reads rather than storing garbage. A thin,
            # one-sided book (ask-only, few offers) can't be junk-filtered —
            # with 2 offers the junk IS the cluster — so its "mid" is often
            # nonsense (transmute stored at 155 ex from 2 junk asks). Require
            # either a two-sided book, or enough same-side offers to form a
            # real cluster. Everything cheap and thin gets a bid quote instead,
            # which is the direction people actually trade it.
            two_sided = mb.median_bid is not None and mb.median_ask is not None
            enough = (len(mb.bids) + len(mb.asks)) >= 3
            if not (two_sided or enough):
                continue
            item_id = self.store.upsert_item(
                f"cur:{currency_id}",
                "currency",
                # Use the catalogued display name; passing the raw id here
                # would overwrite "Divine Orb" with "divine".
                display.get(currency_id, currency_id),
                currency_id=currency_id,
            )
            listings = [
                {
                    "listing_hash": o.listing_hash,
                    "account": o.account,
                    "is_online": True,
                    "price_amount": o.price,
                    "price_currency": base,
                    "price_base": o.price,
                    "stock": o.stock,
                    "indexed_at": o.indexed_at,
                    "whisper": o.whisper,
                }
                for o in (mb.asks[:5] + mb.bids[:5])
            ]
            self.store.record_sample(
                item_id,
                league_id,
                ts=ts,
                source="exchange",
                n_listings=len(mb.asks) + len(mb.bids),
                currency=base,
                # low/high are the robust medians (what the series charts);
                # p25/p75 carry the extremes for trade execution.
                low=mb.median_bid,
                p25=mb.best_bid,
                median=mid,
                p75=mb.best_ask,
                high=mb.median_ask,
                price_base=mid,
                base_currency=base,
                total_stock=mb.ask_depth + mb.bid_depth,
                listings=listings,
            )
            priced += 1

        self.store.log_run(
            f"currency:{tier.name}", league, started, "ok", items_priced=priced
        )
        log.info(
            "currency tier %s: priced %d/%d in %s", tier.name, priced, len(ids), league
        )
        return {"tier": tier.name, "priced": priced, "candidates": len(ids)}

    async def sweep_ninja_currency(self, league: str) -> dict[str, Any]:
        """Store currency prices from poe.ninja's in-game exchange data.

        This is the accurate currency source. The GGG trade2 exchange reads the
        near-abandoned bulk-listing market (single-digit offer counts); the
        in-game Currency Exchange, where PoE2 currency actually trades, is
        surfaced only by poe.ninja. Stored with source='ninja' so get_price and
        stash valuation prefer it over the thin trade2 data.
        """
        from ..ggg.ninja import NinjaClient

        started = utcnow()
        base = self.cfg.base_currency
        league_id = self.store.league_id(league, self.cfg.realm)
        ts = utcnow()

        try:
            async with NinjaClient() as ninja:
                quotes = await ninja.currency_quotes(league, base)
        except Exception as exc:
            log.warning("poe.ninja sweep failed: %s", exc)
            self.store.log_run("ninja", league, started, "error", detail=str(exc))
            return {"source": "poe.ninja", "priced": 0, "error": str(exc)}

        priced = 0
        for cid, q in quotes.items():
            if not q.value_in_base or q.value_in_base <= 0:
                continue
            item_id = self.store.upsert_item(
                f"cur:{cid}", "currency", q.name, currency_id=cid,
                category=q.category,
            )
            # Symmetric book: poe.ninja gives a single clearing price, so bid
            # and ask are the same. Volume rides along as depth.
            self.store.record_sample(
                item_id, league_id, ts=ts, source="ninja",
                n_listings=q.volume, currency=base,
                low=q.value_in_base, median=q.value_in_base,
                high=q.value_in_base, price_base=q.value_in_base,
                base_currency=base, total_stock=q.volume,
            )
            priced += 1

        self.store.log_run("ninja", league, started, "ok", items_priced=priced)
        log.info("poe.ninja: priced %d currencies in %s", priced, league)
        return {"source": "poe.ninja", "priced": priced}

    async def scan_watchlist(self, league: str, wl: Watchlist) -> dict[str, Any]:
        """Search + fetch each target in a watchlist, storing one sample each."""
        started = utcnow()
        league_id = self.store.league_id(league, self.cfg.realm)
        priced, failed = 0, 0

        for target in wl.active_targets():
            try:
                point = await self.api.price_target(league, target)
            except Exception as exc:
                failed += 1
                log.warning("target %s failed: %s", target.key, exc)
                continue
            if not point.n_listings:
                continue

            item_id = self.store.upsert_item(
                target.key,
                target.kind,
                target.label or target.describe(),
                name=target.name,
                type_=target.type,
                currency_id=target.currency_id,
                watchlist=wl.name,
            )
            ts = utcnow()
            price_base = self._to_base(point.median, point.currency, league, ts)
            for L in point.listings:
                L["price_base"] = self._to_base(
                    L.get("price_amount"), L.get("price_currency"), league, ts
                )
            self.store.record_sample(
                item_id, league_id, ts=ts, source="search",
                n_listings=point.n_listings, currency=point.currency,
                low=point.low, p25=point.p25, median=point.median,
                p75=point.p75, high=point.high,
                price_base=price_base, base_currency=self.cfg.base_currency,
                listings=point.listings,
            )
            priced += 1

        status = "ok" if not failed else "partial"
        self.store.log_run(
            f"watchlist:{wl.name}", league, started, status,
            items_priced=priced, detail=f"{failed} targets failed" if failed else None,
        )
        log.info("watchlist %s: priced %d, failed %d", wl.name, priced, failed)
        return {"watchlist": wl.name, "priced": priced, "failed": failed}

    async def sweep_pairs(self, league: str, currencies: list[str]) -> dict[str, Any]:
        """Collect direct currency-to-currency rates for a small set.

        One request prices every pair leaving a currency, so N currencies cost
        N requests. Keep the set small (<= 12): this is the input to multi-hop
        arbitrage, and only liquid currencies can actually close a cycle.
        """
        from ..ggg.trade import EXCHANGE_BATCH, dominant_cluster

        started = utcnow()
        league_id = self.store.league_id(league, self.cfg.realm)
        ts = utcnow()
        rows: list[dict[str, Any]] = []

        skipped: list[str] = []
        unanchored: list[str] = []
        for frm in currencies:
            candidates = []
            for to in currencies:
                if to == frm:
                    continue
                ratio = self._implied_rate(frm, to, league, ts)
                # Unknown ratio: keep it, we have nothing to judge it on yet.
                if ratio and (
                    ratio > PAIR_MAX_VALUE_RATIO
                    or ratio < 1.0 / PAIR_MAX_VALUE_RATIO
                ):
                    skipped.append(f"{frm}->{to}")
                    continue
                candidates.append(to)

            targets = candidates[:EXCHANGE_BATCH]
            if not targets:
                continue
            try:
                offers = await self.api.exchange_raw(league, [frm], targets)
            except Exception as exc:
                log.warning("pair sweep %s failed: %s", frm, exc)
                continue

            by_to: dict[str, list[tuple[float, int]]] = {}
            for o in offers:
                # We give `frm`; the lister hands over what we receive.
                if o.takes_currency != frm or not o.takes_amount:
                    continue
                by_to.setdefault(o.gives_currency, []).append(
                    (o.gives_amount / o.takes_amount, o.stock)
                )

            for to, pairs in by_to.items():
                prices = [r for r, _ in pairs]
                kept = set(dominant_cluster(prices))
                real = [(r, st) for r, st in pairs if r in kept]

                # A pair often has only a handful of offers and every one of
                # them can be junk — observed live: "mirror -> alch = 1.0" and
                # "divine -> chaos = 1.0", where a lister offers 1 of something
                # for 1 of anything. With no good offers present the cluster
                # simply returns the junk, so the pair cannot police itself.
                #
                # The base-relative prices are a far better anchor: they come
                # from a two-sided book with many more offers. Anything wildly
                # inconsistent with the implied cross-rate is discarded. The
                # band is deliberately wide, because a genuine dislocation is
                # exactly what this data is for.
                expected = self._implied_rate(frm, to, league, ts)
                if not expected:
                    # With no anchor there is no way to tell a real rate from
                    # "1 of mine for 1 of yours" bait, and unanchored rows were
                    # observed passing straight through as divine->annul = 1.0.
                    # Storing nothing is strictly better than storing junk: the
                    # pair is simply picked up once both sides have a price.
                    unanchored.append(f"{frm}->{to}")
                    continue
                real = [
                    (r, st) for r, st in real
                    if expected / PAIR_PLAUSIBILITY <= r
                    <= expected * PAIR_PLAUSIBILITY
                ]
                if not real:
                    continue
                rates = sorted(r for r, _ in real)
                rows.append(
                    {
                        "from": frm,
                        "to": to,
                        # Median is the honest rate; best is what a single
                        # execution could actually achieve right now.
                        "rate": rates[len(rates) // 2],
                        "best_rate": rates[-1],
                        "depth": sum(st for _, st in real),
                        "n_offers": len(real),
                        "n_raw": len(pairs),
                        "implied": expected,
                    }
                )

        written = self.store.record_pair_rates(league_id, ts, rows)
        self.store.log_run(
            "pairs", league, started, "ok", items_priced=written,
            detail=(f"{len(skipped)} skipped on value ratio, "
                    f"{len(unanchored)} skipped as unanchored"),
        )
        log.info(
            "pair sweep: %d direct rates across %d currencies "
            "(%d pairs skipped as unmarketable)",
            written, len(currencies), len(skipped),
        )
        return {
            "pairs": written,
            "currencies": len(currencies),
            "skipped_unmarketable": len(skipped),
            "skipped_unanchored": len(unanchored),
            "skipped_examples": skipped[:8],
        }

    def _implied_rate(
        self, frm: str, to: str, league: str, at: datetime
    ) -> float | None:
        """Cross-rate implied by the two base-relative prices.

        Used only as a plausibility anchor, never as the recorded rate — the
        whole point of a direct quote is that it may legitimately differ.
        """
        base = self.cfg.base_currency
        frm_px = 1.0 if frm == base else self.store.currency_rate(frm, league, at)
        to_px = 1.0 if to == base else self.store.currency_rate(to, league, at)
        if not frm_px or not to_px:
            return None
        return frm_px / to_px

    def _to_base(
        self, amount: float | None, currency: str | None, league: str, at: datetime
    ) -> float | None:
        """Convert a listed price into the base currency.

        Uses the rate recorded nearest to ``at``, so a historical row converts
        at the rate that applied then rather than today's.
        """
        if amount is None or not currency:
            return None
        if currency == self.cfg.base_currency:
            return float(amount)
        rate = self.store.currency_rate(currency, league, at)
        return float(amount) * rate if rate else None

    def maintain(self) -> dict[str, Any]:
        """Roll up recent samples, then prune what the rollups now cover."""
        started = utcnow()
        rolled = self.store.rollup()
        pruned = self.store.prune(self.cfg.raw_retention_days)
        self.store.log_run(
            "maintain", None, started, "ok",
            detail=f"rollup={rolled} prune={pruned}",
        )
        return {"rollup": rolled, "prune": pruned}
