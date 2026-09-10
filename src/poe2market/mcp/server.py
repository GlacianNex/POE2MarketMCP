"""The MCP server: read access to collected history, plus live market calls.

Two classes of tool live here and they behave differently:

* **History tools** (``get_price_history``, ``get_movers``) read only the local
  database. They are instant, free, and bounded by what the collector has
  gathered so far.
* **Live tools** (``find_listings``, ``prepare_trade``) call GGG directly and
  spend from the same shared rate budget as the collector.

Prices are reported with their spread and a confidence label rather than as a
bare number. PoE2's trade-site exchange is thin — a currency can show a 90%
bid/ask spread — and a midpoint quoted without that context would be actively
misleading to anything trading on it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from ..config import Config, load_config, load_watchlists
from ..ggg.client import GGGClient
from ..ggg.trade import TradeAPI
from ..config import WatchTarget
from ..ratelimit import RateLimiter
from ..store.db import Store

log = logging.getLogger("poe2market.mcp")

server = MCPServer(
    name="poe2market",
    instructions=(
        "Path of Exile 2 market data, sourced from GGG's own trade API.\n"
        "\n"
        "READ FIRST: poe2market://guide (how to answer correctly) and "
        "poe2market://tools (every tool, its arguments and return shape). "
        "poe2market://state reports what is currently collected, which "
        "league is active, and how much rate budget is left. Also available: "
        "poe2market://setup, poe2market://data-model, "
        "poe2market://rate-limits.\n"
        "\n"
        "Core rules:\n"
        "1. Prefer get_price / get_price_history (local, free, instant) over "
        "find_listings / prepare_trade (live, spends a rate budget shared with "
        "the background collector; starving it permanently damages history).\n"
        "2. Always surface the `confidence` field. PoE2 order books are thin; "
        "a midpoint between a bid and an ask 90% apart is not a tradeable "
        "price. For `very-low`, say it is one seller's asking price.\n"
        "3. Never mix currencies. `price` is in `price_currency`; `listed_*` "
        "fields are in `listed_in`. Confusing them errs by ~375x.\n"
        "4. Empty history means NOT YET COLLECTED, not 'no market'. History "
        "starts when the collector first ran and cannot be backfilled. Check "
        "market_status before drawing conclusions.\n"
        "5. Each league is a separate economy; never carry a price across "
        "leagues. Omitting `league` uses the active challenge league.\n"
        "6. No API executes a trade. Tools return a `whisper` string for a "
        "human to send in game. Never claim a trade was sent or completed.\n"
        "7. Item keys, not names: resolve wording via search_items first "
        "(`cur:divine`, `uniq:mageblood`)."
    ),
)

_cfg: Config | None = None
_store: Store | None = None
_limiter: RateLimiter | None = None


def cfg() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(cfg().db_path)
    return _store


def limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(str(cfg().db_path))
    return _limiter


def _client() -> GGGClient:
    """A fresh client per live call; the rate ledger is what carries state."""
    return GGGClient(cfg().user_agent, limiter())


def _confidence(n_listings: int, spread_pct: float | None) -> str:
    """Label how much weight a quoted price deserves."""
    if n_listings <= 1:
        return "very-low: single listing"
    if spread_pct is None:
        return "low: one-sided market (no bid or no ask)"
    if spread_pct > 40:
        return f"low: {spread_pct:.0f}% bid/ask spread"
    if spread_pct > 15:
        return f"medium: {spread_pct:.0f}% spread"
    return f"high: {spread_pct:.0f}% spread, {n_listings} listings"


def _default_league() -> str:
    """The league every tool falls back to when none is named.

    Prefers what the collector resolved, so `@current` in config means the
    tools follow the live league without a restart or an API call here.
    """
    from ..ggg.leagues import cached_leagues

    resolved = cached_leagues(store())
    if resolved:
        return resolved[0]
    concrete = [x for x in cfg().leagues if not x.startswith("@")]
    if concrete:
        return concrete[0]
    raise ValueError(
        "No league resolved yet. Start the collector, or set an explicit "
        "league in config.toml."
    )


# -- reference ----------------------------------------------------------


@server.tool()
async def list_leagues() -> dict[str, Any]:
    """List PoE2 leagues available on the trade API, and which are collected.

    Use this first when the user names a league loosely ("the new league"), so
    later calls pass an exact league id.
    """
    async with _client() as c:
        leagues = await TradeAPI(c).leagues()
    tracked = set(cfg().leagues)
    return {
        "leagues": [
            {
                "id": L.get("id"),
                "text": L.get("text"),
                "realm": L.get("realm"),
                "collected": L.get("id") in tracked,
            }
            for L in leagues
        ],
        "default": cfg().leagues[0] if cfg().leagues else None,
    }


@server.tool()
async def market_status() -> dict[str, Any]:
    """Report collector health, database coverage and remaining rate budget.

    Check this before concluding that missing data means a missing market —
    an empty history usually means the collector has not been running.
    """
    st = store().stats()
    with store().conn() as c:
        runs = [
            dict(r)
            for r in c.execute(
                "SELECT job, league, started_at, status, items_priced, detail "
                "FROM collector_run ORDER BY started_at DESC LIMIT 10"
            ).fetchall()
        ]
    return {
        "database": {
            "path": str(cfg().db_path),
            "size_bytes": st["db_bytes"],
            "items_catalogued": st["item"],
            "raw_samples": st["price_sample"],
            "hourly_candles": st["price_hourly"],
            "daily_candles": st["price_daily"],
            "earliest_sample": st["earliest_sample"],
            "latest_sample": st["latest_sample"],
        },
        "leagues": st["leagues"],
        "recent_collector_runs": runs,
        "rate_budget": limiter().budget("trade-exchange")
        + limiter().budget("trade-search"),
        "note": (
            "History begins when the collector first ran; GGG serves only "
            "current listings, so earlier data cannot be backfilled."
        ),
    }


@server.tool()
async def search_items(
    query: str = "",
    kind: Literal["", "currency", "unique", "base", "raw"] = "",
    watchlist: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Find tracked items by name, returning the item keys other tools take.

    Every priceable thing has a stable key (``cur:divine``, or a watchlist
    target's key). Resolve a user's wording to a key here first.
    """
    rows = store().list_items(
        kind=kind or None,
        watchlist=watchlist or None,
        query=query or None,
        limit=limit,
    )
    return {"count": len(rows), "items": rows}


# -- prices -------------------------------------------------------------


@server.tool()
async def get_price(item_key: str, league: str = "") -> dict[str, Any]:
    """Latest recorded price for one item, with spread and confidence.

    Reads the local database only. Returns ``found: false`` when the collector
    has not yet priced this item, which is not the same as the item being
    unsellable — check ``market_status``.
    """
    league = league or _default_league()
    row = store().latest(item_key, league)
    if not row:
        return {
            "found": False,
            "item_key": item_key,
            "league": league,
            "hint": "Not yet collected. Try find_listings for a live lookup.",
        }

    # low/high mean different things per source, and conflating them mixes
    # currencies. Exchange rows are a two-sided book in the base currency;
    # search rows are percentiles denominated in whatever the sellers listed.
    source = row.get("source")
    n = row.get("n_listings") or 0
    listed_currency = row.get("currency")
    out: dict[str, Any] = {
        "found": True,
        "item_key": item_key,
        "label": row.get("label"),
        "league": league,
        "as_of": row.get("ts"),
        "source": source,
        "price": row.get("price_base"),
        "price_currency": row.get("base_currency"),
        "listings_seen": n,
    }

    if source == "exchange":
        bid, ask = row.get("low"), row.get("high")
        mid = row.get("price_base")
        spread = ((ask - bid) / mid * 100.0) if (bid and ask and mid) else None
        out.update(
            {
                "quote_type": "two-sided book",
                "best_bid": bid,
                "best_ask": ask,
                "spread_pct": round(spread, 1) if spread is not None else None,
                "depth": row.get("total_stock"),
                "confidence": _confidence(n, spread),
            }
        )
    else:
        out.update(
            {
                "quote_type": "ask-side listings only",
                "listed_in": listed_currency,
                "listed_low": row.get("low"),
                "listed_median": row.get("median"),
                "listed_high": row.get("high"),
                "note": (
                    f"low/median/high are asking prices in {listed_currency}; "
                    f"`price` is the median converted to "
                    f"{row.get('base_currency')}. There is no bid side, so no "
                    "spread can be computed."
                ),
                "confidence": _confidence(n, None),
            }
        )
    return out


@server.tool()
async def get_price_history(
    item_key: str,
    league: str = "",
    days: int = 30,
    resolution: Literal["auto", "hourly", "daily"] = "auto",
) -> dict[str, Any]:
    """OHLC price history for one item, for charting or trend analysis.

    Reads rollup candles, so long windows stay fast. ``auto`` uses hourly
    candles up to 14 days and daily beyond that.
    """
    league = league or _default_league()
    candles = store().history(
        item_key, league, days=days, resolution=resolution
    )
    if not candles:
        return {
            "item_key": item_key,
            "league": league,
            "candles": [],
            "hint": (
                "No history yet. Rollups run every "
                f"{cfg().rollup_cadence_minutes} min, and history only exists "
                "from when the collector started."
            ),
        }

    first, last = candles[0], candles[-1]
    change = None
    if first.get("open"):
        change = (last["close"] - first["open"]) / first["open"] * 100.0
    return {
        "item_key": item_key,
        "league": league,
        "resolution": ("hourly" if resolution == "hourly"
                       or (resolution == "auto" and days <= 14) else "daily"),
        "base_currency": last.get("base_currency"),
        "candles": candles,
        "summary": {
            "from": first["bucket"],
            "to": last["bucket"],
            "open": first.get("open"),
            "close": last.get("close"),
            "high": max((c["high"] for c in candles if c["high"] is not None), default=None),
            "low": min((c["low"] for c in candles if c["low"] is not None), default=None),
            "pct_change": round(change, 1) if change is not None else None,
        },
    }


@server.tool()
async def get_movers(
    league: str = "", days: int = 1, limit: int = 20, min_samples: int = 3
) -> dict[str, Any]:
    """Items with the largest percentage price moves over a window.

    Needs at least ``days + 1`` days of collected history to be meaningful.
    """
    league = league or _default_league()
    rows = store().movers(
        league, days=days, limit=limit, min_samples=min_samples
    )
    return {
        "league": league,
        "window_days": days,
        "count": len(rows),
        "movers": [
            {**r, "pct_change": round(r["pct_change"], 1)} for r in rows
        ],
    }


# -- live market --------------------------------------------------------


@server.tool()
async def find_listings(
    name: str = "",
    type: str = "",
    league: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Search live trade listings right now, returning prices and whisper text.

    Spends the shared GGG rate budget (600 searches / 6h), so prefer
    ``get_price`` when recorded data is good enough.

    The returned ``whisper`` is the message to send in game. This server cannot
    send it: GGG exposes no trade-execution API, and automating in-game input
    violates the terms of service. A human completes the trade.
    """
    league = league or _default_league()
    if not name and not type:
        return {"error": "Provide at least one of `name` or `type`."}

    target = WatchTarget(
        key=f"adhoc:{name or type}",
        label=name or type,
        kind="unique" if name else "base",
        name=name or None,
        type=type or None,
        sample_size=max(1, min(limit, 20)),
    )
    async with _client() as c:
        point = await TradeAPI(c).price_target(league, target)

    listings = [
        {
            "account": L.get("account"),
            "online": L.get("is_online"),
            "price": L.get("price_amount"),
            "currency": L.get("price_currency"),
            "indexed": L.get("indexed_at"),
            "whisper": L.get("whisper"),
            "item_name": (L.get("item") or {}).get("name"),
            "item_type": (L.get("item") or {}).get("typeLine"),
        }
        for L in point.listings
    ]
    listings.sort(key=lambda L: (L["price"] is None, L["price"]))

    return {
        "league": league,
        "searched": name or type,
        "count": len(listings),
        "price_stats": {
            "currency": point.currency,
            "low": point.low,
            "median": point.median,
            "high": point.high,
        },
        "listings": listings[:limit],
        "trade_note": (
            "Send `whisper` in game to contact the seller. This server never "
            "sends whispers or automates trades."
        ),
    }


@server.tool()
async def prepare_trade(
    item_key: str = "",
    name: str = "",
    league: str = "",
    max_price: float = 0.0,
) -> dict[str, Any]:
    """Pick the best current listing for an item and return a ready whisper.

    Cross-checks the live ask against recorded history so an outlier price is
    flagged before you commit. Returns the whisper for a human to send; it does
    not contact anyone.
    """
    league = league or _default_league()
    search_name = name
    if item_key and not search_name:
        rows = store().list_items(query=item_key, limit=1)
        search_name = (rows[0]["label"] if rows else item_key)

    live = await find_listings(name=search_name, league=league, limit=10)
    if live.get("error") or not live.get("listings"):
        return {"ok": False, "reason": "No live listings found.", "detail": live}

    affordable = [
        L for L in live["listings"]
        if L["price"] is not None and (not max_price or L["price"] <= max_price)
    ]
    if not affordable:
        return {
            "ok": False,
            "reason": f"No listing at or below {max_price}.",
            "cheapest": live["listings"][0],
        }

    best = affordable[0]
    recorded = store().latest(item_key, league) if item_key else None
    fair = recorded.get("price_base") if recorded else None
    verdict = "no recorded history to compare against"
    if fair and best["price"]:
        delta = (best["price"] - fair) / fair * 100.0
        verdict = (
            f"{abs(delta):.0f}% {'above' if delta > 0 else 'below'} "
            f"the recorded price of {fair:.2f}"
        )

    return {
        "ok": True,
        "league": league,
        "item": search_name,
        "chosen": best,
        "price_check": verdict,
        "whisper": best.get("whisper"),
        "next_step": (
            "Send the whisper in game, then complete the trade in the trade "
            "window. Nothing is sent automatically."
        ),
    }


# -- watchlists ---------------------------------------------------------


@server.tool()
async def list_watchlists() -> dict[str, Any]:
    """Show configured watchlists: what is scanned, how often, and priority."""
    lists = load_watchlists(cfg())
    return {
        "watchlist_dir": str(cfg().watchlist_dir),
        "watchlists": [
            {
                "name": w.name,
                "description": w.description,
                "cadence_minutes": w.cadence_minutes,
                "priority": w.priority,
                "target_count": len(w.active_targets()),
                "targets": [
                    {"key": t.key, "label": t.label, "kind": t.kind,
                     "name": t.name, "type": t.type}
                    for t in w.active_targets()
                ],
            }
            for w in lists
        ],
        "currency_tiers": [
            {"name": t.name, "categories": t.categories,
             "cadence_minutes": t.cadence_minutes}
            for t in cfg().currency_tiers
        ],
    }


@server.tool()
async def find_arbitrage(
    league: str = "", min_profit_pct: float = 5.0, limit: int = 20
) -> dict[str, Any]:
    """Find currencies where the median bid exceeds the median ask.

    Deliberately compares **medians, not extremes**. The cheapest ask on this
    endpoint is very often a fat-finger or a sold-but-still-listed order: the
    median listing is ~85 minutes old when GGG serves it. Divine was observed
    with asks of 100/188/260/300 against bids of 230/200/180/162/160 — the
    extremes cross by 130 exalted and imply free money, while the medians show
    an ordinary 26% spread. Screening on extremes would report a large
    opportunity in a market that has none.

    A crossing that survives at the median is a genuine dislocation. Even then
    treat it as a lead, not a filled trade: both counterparties must be online,
    stock is finite, and these complete by whisper and a manual trade window,
    so the price can move before anyone replies.

    ``stale_extremes`` reports books that cross only at the extremes, which is
    a staleness signal rather than an opportunity.
    """
    league = league or _default_league()
    with store().conn() as c:
        rows = c.execute(
            """
            SELECT i.key, i.label,
                   s.low  AS bid,        -- median bid
                   s.high AS ask,        -- median ask
                   s.p25  AS best_bid,   -- extreme, for staleness detection
                   s.p75  AS best_ask,
                   s.price_base AS mid, s.total_stock, s.ts, s.base_currency
            FROM price_sample s
            JOIN item i ON i.id = s.item_id
            JOIN league g ON g.id = s.league_id
            WHERE g.name = ? AND s.source = 'exchange'
              AND s.low IS NOT NULL AND s.high IS NOT NULL
              AND s.id IN (
                  SELECT MAX(id) FROM price_sample
                  WHERE league_id = s.league_id GROUP BY item_id
              )
            """,
            (league,),
        ).fetchall()

    hits, stale = [], []
    for r in rows:
        bid, ask = r["bid"], r["ask"]
        best_bid, best_ask = r["best_bid"], r["best_ask"]

        # Crossing only at the extremes means one stale or mistyped order.
        if best_bid and best_ask and best_bid > best_ask and not (
            bid and ask and bid > ask
        ):
            stale.append(
                {
                    "item_key": r["key"],
                    "label": r["label"],
                    "extreme_bid": best_bid,
                    "extreme_ask": best_ask,
                    "median_bid": bid,
                    "median_ask": ask,
                    "why": "crosses only at the extremes; likely stale or mistyped",
                }
            )
            continue

        if not (bid and ask) or bid <= ask:
            continue
        profit_pct = (bid - ask) / ask * 100.0
        if profit_pct < min_profit_pct:
            continue
        hits.append(
            {
                "item_key": r["key"],
                "label": r["label"],
                "buy_at": ask,
                "sell_at": bid,
                "profit_per_unit": round(bid - ask, 4),
                "profit_pct": round(profit_pct, 1),
                "currency": r["base_currency"],
                "depth": r["total_stock"],
                "as_of": r["ts"],
            }
        )
    hits.sort(key=lambda h: h["profit_pct"], reverse=True)
    return {
        "league": league,
        "count": len(hits),
        "opportunities": hits[:limit],
        "stale_extremes": stale[:limit],
        "caveat": (
            "Leads, not filled trades. Both counterparties must be online and "
            "responsive; PoE2 trades complete by whisper and a manual trade "
            "window, so prices can move before anyone replies."
        ),
    }


@server.tool()
async def get_stash_value(league: str = "", refresh: bool = False) -> dict[str, Any]:
    """Value your own stash against collected prices.

    Reads the account's **public listings** (no login) — the working path on
    PoE2, since the authenticated stash API is OAuth-only and closed. Only
    publicly-listed items are visible; set the account with `stash_account` in
    config or POE2MARKET_ACCOUNT.

    Set ``refresh`` to pull fresh listings; otherwise the latest snapshot is
    returned.
    """
    league = league or _default_league()

    if refresh:
        from ..collect.stash import snapshot_public_listings

        return await snapshot_public_listings(cfg(), store(), league)

    with store().conn() as c:
        snap = c.execute(
            "SELECT s.* FROM stash_snapshot s JOIN league g ON g.id = s.league_id "
            "WHERE g.name = ? ORDER BY s.ts DESC LIMIT 1",
            (league,),
        ).fetchone()
        if not snap:
            return {
                "ok": False,
                "reason": (
                    "No stash snapshot yet. Call with refresh=true, or run "
                    "`poe2market stash-auth` then `poe2market stash` if no "
                    "credential is stored."
                ),
            }
        top = [
            dict(r) for r in c.execute(
                "SELECT type_line, stack_size, unit_base, value_base, tab_name "
                "FROM stash_item WHERE snapshot_id = ? AND value_base IS NOT NULL "
                "ORDER BY value_base DESC LIMIT 15",
                (snap["id"],),
            ).fetchall()
        ]

    return {
        "ok": True,
        "league": league,
        "as_of": snap["ts"],
        "tabs": snap["tab_count"],
        "items": snap["item_count"],
        "total_value": snap["total_base"],
        "base_currency": snap["base_currency"],
        "valuation_side": "bid (realisable, not midpoint)",
        "top_holdings": top,
    }


@server.tool()
async def get_stash_history(league: str = "", limit: int = 30) -> dict[str, Any]:
    """Track how your stash's total value has changed over time.

    Each snapshot is valued at the prices that applied when it was taken, so
    the series separates 'I acquired more' from 'what I hold got dearer'.
    """
    league = league or _default_league()
    with store().conn() as c:
        rows = [
            dict(r) for r in c.execute(
                "SELECT s.ts, s.total_base, s.item_count, s.base_currency "
                "FROM stash_snapshot s JOIN league g ON g.id = s.league_id "
                "WHERE g.name = ? ORDER BY s.ts DESC LIMIT ?",
                (league, limit),
            ).fetchall()
        ]
    rows.reverse()
    change = None
    if len(rows) >= 2 and rows[0]["total_base"]:
        change = (rows[-1]["total_base"] - rows[0]["total_base"]) / rows[0]["total_base"] * 100.0
    return {
        "league": league,
        "snapshots": rows,
        "pct_change": round(change, 1) if change is not None else None,
    }


@server.tool()
async def list_stash_items(
    league: str = "",
    search: str = "",
    tab: str = "",
    rarity: str = "",
    priced_only: bool = False,
    group_stacks: bool = True,
    sort: Literal["value", "quantity", "name"] = "value",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """List what is actually in your stash: every item, with quantities.

    Reads the most recent snapshot. Set ``group_stacks`` (the default) to
    aggregate the same item across tabs — 3 stacks of Exalted Orbs in three
    tabs become one row with the combined quantity, which is almost always the
    question being asked. Turn it off to see each physical stack and its tab.

    Unpriced items (gear, maps, anything without a collected price) are
    included with a null value rather than dropped, so the list is a real
    inventory rather than only the part we happen to have priced.

    Filters: ``search`` matches name or base type, ``tab`` matches a tab name,
    ``rarity`` is Normal/Magic/Rare/Unique/Currency/Gem.
    """
    league = league or _default_league()

    snap = store().latest_stash_snapshot(league)
    if not snap:
        return {
            "ok": False,
            "reason": (
                "No stash snapshot yet. Run `poe2market stash-auth` then "
                "`poe2market stash`, or call get_stash_value(refresh=true)."
            ),
        }

    rows, totals = store().stash_items(
        snap["id"],
        search=search or None,
        tab=tab or None,
        rarity=rarity or None,
        priced_only=priced_only,
        group_stacks=group_stacks,
        sort=sort,
        limit=limit,
        offset=offset,
    )

    return {
        "ok": True,
        "league": league,
        "as_of": snap["ts"],
        "base_currency": snap["base_currency"],
        "grouped": group_stacks,
        "matched": {
            "rows_returned": len(rows),
            "stacks": totals["stacks"],
            "units": totals["units"],
            "value": round(totals["value"], 2) if totals["value"] else 0.0,
        },
        "snapshot_totals": {
            "items": snap["item_count"],
            "value": snap["total_base"],
        },
        "items": [
            {
                "item": r["display"],
                "base_type": r["type_line"],
                "rarity": r["rarity"],
                "quantity": r["quantity"],
                "stacks": r["stacks"],
                "unit_value": round(r["unit_value"], 4) if r["unit_value"] else None,
                "total_value": round(r["total_value"], 2) if r["total_value"] else None,
                "tabs": r["tabs"],
                "priced": r["priced_from"] != "unpriced",
            }
            for r in rows
        ],
        "note": (
            "unit_value is the bid side — what you could realise, not a "
            "midpoint. Items with priced=false have no collected price yet."
        ),
    }


# -- documentation resources -------------------------------------------
#
# Served from docs/ so the files are the single source of truth: what a
# connecting client reads is exactly what a human reads in the repository.


def _doc(name: str) -> str:
    from ..config import DEFAULT_ROOT

    # Only agent-facing docs are served; maintainer docs live in
    # docs/maintainers/ and are deliberately not exposed over MCP.
    path = DEFAULT_ROOT / "docs" / "agent" / name
    if not path.exists():
        return f"(missing: docs/{name})"
    return path.read_text()


@server.resource(
    "poe2market://guide",
    name="Agent guide",
    description=(
        "How to use this server correctly: tool selection, item keys, reading "
        "prices without mixing currencies, confidence handling, and the "
        "mistakes that produce wrong answers. Read this first."
    ),
    mime_type="text/markdown",
)
def guide_resource() -> str:
    return _doc("AGENT_GUIDE.md")


@server.resource(
    "poe2market://data-model",
    name="Data model",
    description=(
        "Storage tiers, price semantics, bid/ask direction, side-aware "
        "conversion, and what OHLC candle fields mean."
    ),
    mime_type="text/markdown",
)
def data_model_resource() -> str:
    return _doc("DATA_MODEL.md")


@server.resource(
    "poe2market://rate-limits",
    name="Rate limits and etiquette",
    description=(
        "Measured GGG limits, why the budget is shared with the collector, and "
        "the compliance position on the undocumented trade API."
    ),
    mime_type="text/markdown",
)
def rate_limits_resource() -> str:
    return _doc("RATE_LIMITS.md")


@server.resource(
    "poe2market://state",
    name="Live server state",
    description=(
        "What is collected right now: active league, coverage window, tracked "
        "item counts, watchlists, and remaining rate budget. Check this before "
        "concluding that missing data means a missing market."
    ),
    mime_type="application/json",
)
def state_resource() -> dict[str, Any]:
    from ..ggg.leagues import cached_leagues

    st = store().stats()
    try:
        default = _default_league()
    except ValueError:
        default = None

    with store().conn() as c:
        tracked = {
            r["kind"]: r["n"]
            for r in c.execute(
                "SELECT kind, COUNT(*) n FROM item GROUP BY kind"
            )
        }
        priced = c.execute(
            "SELECT COUNT(DISTINCT item_id) n FROM price_sample"
        ).fetchone()["n"]
        last_run = c.execute(
            "SELECT job, status, started_at FROM collector_run "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    return {
        "default_league": default,
        "collected_leagues": cached_leagues(store()) or cfg().leagues,
        "base_currency": cfg().base_currency,
        "coverage": {
            "earliest_sample": st["earliest_sample"],
            "latest_sample": st["latest_sample"],
            "raw_samples": st["price_sample"],
            "hourly_candles": st["price_hourly"],
            "daily_candles": st["price_daily"],
            "items_catalogued": st["item"],
            "items_with_a_price": priced,
            "tracked_by_kind": tracked,
            "stash_snapshots": st["stash_snapshot"],
        },
        "watchlists": [
            {"name": w.name, "cadence_minutes": w.cadence_minutes,
             "targets": len(w.active_targets())}
            for w in load_watchlists(cfg())
        ],
        "last_collector_run": dict(last_run) if last_run else None,
        "rate_budget": {
            "exchange": limiter().budget("trade-exchange"),
            "search": limiter().budget("trade-search"),
        },
        "reminders": [
            "History starts at earliest_sample; it cannot be backfilled.",
            "Live tools share the rate budget with the collector.",
            "Read poe2market://guide before answering non-trivial questions.",
        ],
    }


@server.tool()
async def find_multi_step_arbitrage(
    league: str = "",
    max_hops: int = 4,
    min_profit_pct: float = 1.0,
    max_age_minutes: int = 120,
    limit: int = 15,
) -> dict[str, Any]:
    """Find profitable trade loops, e.g. chaos -> exalted -> divine -> chaos.

    Pure analysis over already-collected direct pair rates: no API calls, so it
    is free to run as often as you like.

    Only *direct* pair quotes can produce a cycle. Cross-rates synthesised
    through the base currency pay the spread on every leg, so a loop built from
    them always loses — profit appears only where the market's own
    chaos->divine rate has drifted from chaos->exalted->divine.

    Each leg's rate has already paid its own spread and has had junk listings
    filtered out, so a gain above 1.0 is real profit rather than mid-price
    arithmetic. `min_depth` bounds how much can actually be pushed through the
    tightest leg.

    As with all trades here: these complete by whisper and a manual trade
    window. Every counterparty must be online and willing. Treat results as
    leads, and note that a four-hop loop needs four separate humans to answer.
    """
    from ..analysis.arbitrage import Edge, direct_vs_routed, find_cycles

    league = league or _default_league()
    rows = store().latest_pair_rates(league, max_age_minutes=max_age_minutes)
    if not rows:
        return {
            "ok": False,
            "reason": (
                "No direct pair rates collected yet. Set `pair_currencies` in "
                "config.toml and let the collector run, or check that the "
                "rates are not older than max_age_minutes."
            ),
        }

    edges = [
        Edge(
            frm=r["from_currency"], to=r["to_currency"], rate=r["rate"],
            depth=r["depth"] or 0, n_offers=r["n_offers"] or 0, ts=r["ts"],
        )
        for r in rows
    ]
    cycles = find_cycles(
        edges, max_hops=max_hops, min_profit_pct=min_profit_pct
    )
    routed = [
        d for d in direct_vs_routed(edges, cfg().base_currency)
        if d["advantage_pct"] >= min_profit_pct
    ]

    return {
        "ok": True,
        "league": league,
        "edges_considered": len(edges),
        "rates_as_of": max((r["ts"] for r in rows), default=None),
        "cycles": [
            {
                "path": c.describe(),
                "hops": len(c.edges),
                "gain_multiplier": round(c.gain, 5),
                "profit_pct": round(c.profit_pct, 2),
                "min_depth": c.min_depth,
                "legs": [
                    {"from": e.frm, "to": e.to, "rate": round(e.rate, 6),
                     "depth": e.depth, "offers": e.n_offers}
                    for e in c.edges
                ],
            }
            for c in cycles[:limit]
        ],
        "direct_beats_routing": [
            {**d, "advantage_pct": round(d["advantage_pct"], 2)}
            for d in routed[:limit]
        ],
        "caveat": (
            "Leads, not filled trades. Each hop is a separate manual whisper "
            "trade; a 4-hop loop needs four people to answer while prices hold."
        ),
    }


@server.resource(
    "poe2market://tools",
    name="Tool reference",
    description=(
        "Every tool: real signatures, defaults, return shapes with worked "
        "examples, which are free versus which spend the rate budget, and the "
        "mistakes that produce wrong answers."
    ),
    mime_type="text/markdown",
)
def tools_resource() -> str:
    return _doc("TOOL_REFERENCE.md")


@server.resource(
    "poe2market://setup",
    name="Setup and operations",
    description=(
        "Install, configure, run the collector, define watchlists, enable "
        "stash access, and troubleshoot. Read when a tool reports missing "
        "data or a credential problem."
    ),
    mime_type="text/markdown",
)
def setup_resource() -> str:
    return _doc("SETUP.md")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run()


if __name__ == "__main__":
    main()
