"""Read an account's public listings and value them.

The credential-free path to "my stash" on PoE2. The authenticated stash API is
OAuth-only and GGG is not issuing new applications; the POESESSID cookie is
403-forbidden for stash. But the public trade search filters by account name
with no auth at all, so all that is needed is the account handle.

The inherent tradeoff: only items in tabs the owner has marked public/indexed
are visible. Private tabs are not — nothing short of the closed OAuth API can
see those.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import Config
from ..ggg.client import GGGClient
from ..ggg.trade import TradeAPI
from ..ratelimit import RateLimiter
from ..store.db import Store, iso, utcnow

log = logging.getLogger(__name__)

# Currencies a real price is actually quoted in. Items "priced" in anything
# else — waystones, essences, other drops — are placeholder/bulk listings, not
# genuine sale prices. The trade UI shows an unpriced tab as "99 <tab default
# currency>", which is why whole tabs come back as "99 waystone-10".
VALUATION_CURRENCIES = {
    "exalted", "divine", "chaos", "annul", "regal", "vaal", "alch",
    "gcp", "artificers", "mirror", "fracturing-orb", "perfect-jewellers-orb",
    "greater-exalted-orb", "perfect-exalted-orb", "hinekoras-lock",
}

# The trade UI's "no price set" sentinel: the slider maxes at 99, and unpriced
# tabs inherit a non-trade default currency.
UNPRICED_AMOUNT = 99


def is_real_price(amount: Any, currency: Any) -> bool:
    """True when a listing carries a genuine asking price, not a placeholder."""
    if amount is None or not currency:
        return False
    if currency not in VALUATION_CURRENCIES:
        return False
    # 99 of a real currency is occasionally legitimate, but combined with a
    # non-trade currency it is the unpriced sentinel; that case is already
    # excluded above, so a plain 99 exalted is kept.
    return True


async def snapshot_public_listings(
    cfg: Config, store: Store, league: str, account: str | None = None,
    *, include_listed_prices: bool = False,
) -> dict[str, Any]:
    """Snapshot an account's public listings — no credentials required.

    Items are valued in the base currency against collected currency rates
    where possible; each also carries its own listed asking price.
    """
    account = account or cfg.stash_account
    if not account:
        return {
            "ok": False,
            "error": "No account set. Pass one or set stash_account "
                     '(with the "#1234" discriminator).',
        }
    if "#" not in account:
        return {
            "ok": False,
            "error": f"Account {account!r} needs its discriminator, "
                     'e.g. "Name#1234".',
        }

    limiter = RateLimiter(str(cfg.db_path))
    async with GGGClient(cfg.user_agent, limiter) as client:
        try:
            items = await TradeAPI(client).search_by_account(league, account)
        except Exception as exc:
            log.warning("public listing fetch failed: %s", exc)
            return {"ok": False, "error": f"fetch failed: {exc}"}

    if not items:
        return {
            "ok": True, "league": league, "account": account, "items": 0,
            "note": (
                "No public listings found — nothing listed, or tabs not set "
                "public. Private tabs need the OAuth stash API (closed)."
            ),
        }

    # Value currency against poe.ninja's in-game exchange prices (via
    # currency_rate, which prefers the 'ninja' source). Match stash item names
    # to exchange ids through the catalogue.
    with store.conn() as c:
        catalog = {
            (r["label"] or "").lower(): r["currency_id"]
            for r in c.execute(
                "SELECT label, currency_id FROM item "
                "WHERE kind='currency' AND currency_id IS NOT NULL"
            )
        }

    ts = utcnow()
    league_id = store.league_id(league, cfg.realm)

    rate_cache: dict[str, float | None] = {}

    def rate(cid: str | None) -> float | None:
        if not cid:
            return None
        if cid == cfg.base_currency:
            return 1.0
        if cid not in rate_cache:
            rate_cache[cid] = store.currency_rate(cid, league, ts)
        return rate_cache[cid]

    total = 0.0
    priced = 0
    rows: list[dict[str, Any]] = []
    for it in items:
        cid = catalog.get((it.get("type_line") or "").lower())
        unit = rate(cid)
        # Optional: value gear/uniques at the seller's own asking price. Off by
        # default (speculative). Skips the "99 waystone-N" unpriced placeholder.
        if unit is None and include_listed_prices:
            amt, ccy = it.get("price_amount"), it.get("price_currency")
            crate = rate(ccy) if ccy else None
            if amt and crate and not (ccy or "").startswith("waystone"):
                unit = float(amt) * crate
        value = unit * it["stack_size"] if unit is not None else None
        if value:
            total += value
            priced += 1
        rows.append({**it, "unit_base": unit, "value_base": value,
                     "matched_currency": cid})

    # Persist the snapshot so the full inventory is queryable (top_holdings in
    # the return is only a preview) and history builds over time.
    with store.conn() as c:
        cur = c.execute(
            "INSERT INTO stash_snapshot (league_id, ts, account, tab_count, "
            "item_count, total_base, base_currency) VALUES (?,?,?,?,?,?,?)",
            (league_id, iso(ts), account, 0, len(items), total, cfg.base_currency),
        )
        snap_id = cur.lastrowid
        c.executemany(
            "INSERT INTO stash_item (snapshot_id, tab_name, name, type_line, "
            "stack_size, unit_base, value_base, priced_from, rarity, ilvl, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (snap_id, r.get("tab_name"), r.get("name"), r.get("type_line"),
                 r["stack_size"], r.get("unit_base"), r.get("value_base"),
                 "public-listing", r.get("rarity"), r.get("ilvl"),
                 json.dumps(r.get("raw")))
                for r in rows
            ],
        )

    top = sorted(
        (r for r in rows if r["value_base"]),
        key=lambda r: r["value_base"], reverse=True,
    )[:15]
    unpriced = len(items) - priced
    return {
        "ok": True,
        "league": league,
        "account": account,
        "source": "public listings (no auth)",
        "items": len(items),
        "priced_items": priced,
        "unpriced_or_placeholder": unpriced,
        "listed_value": round(total, 2),
        "base_currency": cfg.base_currency,
        "top_holdings": [
            {"item": r["type_line"], "stack": r["stack_size"],
             "unit_ex": round(r["unit_base"], 3),
             "value_ex": round(r["value_base"], 1)}
            for r in top
        ],
        "note": (
            "Valued against live market (currency at bid/sell side, "
            "junk-filtered), not the listed price. Public listings only."
        ),
    }
