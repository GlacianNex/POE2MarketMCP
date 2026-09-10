"""poe.ninja client for PoE2 currency prices.

Why this exists: the GGG trade API's ``exchange`` endpoint reads the old
bulk-listing system, which PoE2 players have largely abandoned in favour of the
in-game Currency Exchange — a currency showed 5 offers there while poe.ninja
reports tens of thousands of trades. The in-game exchange has no public API, so
poe.ninja (which does surface it) is the only accurate source for currency.

Trade2 is still used for items and uniques, where its search returns hundreds
of real listings. This split is deliberate: each source for what it does well.

The endpoint is undocumented-but-public and unversioned; treat it as best
effort. Prices come denominated in Divine Orbs; this module converts to the
configured base currency using the exalted/divine rate from the same payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncio
import time

import httpx

log = logging.getLogger(__name__)

NINJA_BASE = "https://poe.ninja/poe2/api/economy"

#: The base-rate request gates the whole sweep, so it gets its own retries.
BASE_RATE_ATTEMPTS = 3
BASE_RATE_RETRY_DELAY = 2.0

# A browser-like UA and referer; poe.ninja 404s some non-browser requests.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://poe.ninja/poe2/economy",
    "Accept": "application/json",
}

# Currency categories worth pulling. Currency dominates; the rest are optional.
# Full reference incl.
# unique-item categories: docs/maintainers/POE_NINJA_CATEGORIES.md.
# Every exchange category poe.ninja exposes for PoE2. Omens (which can be worth
# hundreds of ex) live under "Ritual". Verified present in a live league.
CURRENCY_TYPES = [
    "Currency", "Ritual", "Fragments", "Essences", "Runes", "Breach",
    "Delirium", "Expedition", "SoulCores", "Abyss", "Idols", "UncutGems",
    "Verisium",
]


@dataclass
class NinjaQuote:
    """One currency's price, both in divine and in the base currency."""

    currency_id: str
    name: str
    value_in_divine: float
    value_in_base: float
    volume: int
    category: str


class NinjaClient:
    def __init__(self, timeout: float = 20.0,
                 retry_delay: float = BASE_RATE_RETRY_DELAY) -> None:
        self.retry_delay = retry_delay
        self.last_response_age: str | None = None
        self._client = httpx.AsyncClient(headers=_HEADERS, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "NinjaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def leagues(self) -> list[str]:
        r = await self._client.get(f"{NINJA_BASE}/leagues")
        r.raise_for_status()
        return [L["id"] for L in r.json()]

    async def _overview(
        self, league: str, type_: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Fetch one category overview.

        ``force`` bypasses the CDN edge cache. A ``Cache-Control: no-cache``
        header alone is not honoured for anonymous requests, so a unique query
        parameter is added to change the cache key — that reliably reaches the
        origin. Only use it for an explicit user-requested refresh; the
        scheduled sweep should ride the cache.
        """
        params: dict[str, Any] = {"league": league, "type": type_}
        headers = {}
        if force:
            params["_"] = int(time.time() * 1000)
            headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        r = await self._client.get(
            f"{NINJA_BASE}/exchange/current/overview",
            params=params, headers=headers or None,
        )
        r.raise_for_status()
        self.last_response_age = r.headers.get("age")
        return r.json()

    async def currency_quotes(
        self, league: str, base_currency: str, *, types: list[str] | None = None,
        fallback_base_in_divine: float | None = None, force: bool = False,
    ) -> dict[str, NinjaQuote]:
        """Fetch currency prices for ``league`` keyed by exchange id.

        Values are converted from poe.ninja's divine-denominated numbers into
        ``base_currency`` using the base's own divine value in the payload.
        """
        quotes: dict[str, NinjaQuote] = {}

        # The base currency's value in divine is quoted only in the Currency
        # category, but is needed to convert every category. Resolve it once
        # from Currency, then apply it to all — otherwise categories without an
        # exalted line (UncutGems, Fragments, ...) get silently skipped.
        # Everything converts through the base currency's divine value, so a
        # failure here would otherwise discard the entire sweep — all
        # categories, hundreds of currencies — over one transient hiccup.
        # Retry, then fall back to the last known rate supplied by the caller.
        base_in_divine = 1.0 if base_currency == "divine" else None
        cached: dict[str, Any] = {}
        if base_in_divine is None:
            for attempt in range(BASE_RATE_ATTEMPTS):
                try:
                    cur = await self._overview(league, "Currency", force=force)
                    cached["Currency"] = cur
                    by_id = {L["id"]: L for L in cur.get("lines", [])}
                    bl = by_id.get(base_currency)
                    base_in_divine = bl.get("primaryValue") if bl else None
                    if base_in_divine:
                        break
                    log.warning(
                        "poe.ninja: %s missing from Currency payload (try %d/%d)",
                        base_currency, attempt + 1, BASE_RATE_ATTEMPTS,
                    )
                except Exception as exc:
                    log.warning(
                        "poe.ninja base-rate lookup failed (try %d/%d): %s",
                        attempt + 1, BASE_RATE_ATTEMPTS, exc,
                    )
                if attempt + 1 < BASE_RATE_ATTEMPTS:
                    await asyncio.sleep(self.retry_delay)

        if not base_in_divine and fallback_base_in_divine:
            # A slightly stale conversion rate is far better than no prices at
            # all: the rate moves on the order of hours, the sweep hourly.
            base_in_divine = fallback_base_in_divine
            log.warning(
                "poe.ninja: using last known %s rate (%.6g divine)",
                base_currency, base_in_divine,
            )

        if not base_in_divine:
            log.warning("poe.ninja: could not resolve %s rate", base_currency)
            return quotes

        for type_ in (types or CURRENCY_TYPES):
            try:
                # The Currency payload was already fetched for the base rate.
                data = cached.get(type_) or await self._overview(
                    league, type_, force=force
                )
            except Exception as exc:
                log.warning("poe.ninja %s/%s failed: %s", league, type_, exc)
                continue

            names = {it["id"]: it.get("name", it["id"])
                     for it in data.get("items", [])}
            cats = {it["id"]: it.get("category", type_)
                    for it in data.get("items", [])}

            for L in data.get("lines", []):
                cid = L["id"]
                v_div = L.get("primaryValue")
                if v_div is None:
                    continue
                quotes[cid] = NinjaQuote(
                    currency_id=cid,
                    name=names.get(cid, cid),
                    value_in_divine=v_div,
                    # divine-value / base's divine-value = value in base units.
                    value_in_base=v_div / base_in_divine,
                    volume=int(L.get("volumePrimaryValue") or 0),
                    category=cats.get(cid, type_),
                )
        return quotes
